#!/usr/bin/env python3
# routerlm.py v0.14 — 2026-10-15
# Changelog (latest two; older entries: `git log -- RouterLM/impl/routerlm.py`):
# - v0.14 (2026-10-15): The Apps box no longer names "a FileMaker script" as an example app (Rene).
# - v0.13 (2026-10-15): Export and Import in Settings (Rene: "exports leave the API keys out"): Export downloads the
#   settings file; Import loads a picked file through the same checks as Save.
"""RouterLM: one endpoint on the office network in front of every model an office uses.

The spec is docs/ARCHITECTURE.md; "section N" in comments points into it.
Python 3.9 or later, standard library only.

What the screens and the config call a lineup, a model, an app and the test questions are Role, Target, Caller and
the check set in the code.

    python3 routerlm.py serve        (then open the address it prints; Settings does the rest)
    python3 routerlm.py check --config routerlm.json
    python3 routerlm.py new-key
    python3 routerlm.py test-questions --config routerlm.json --model local-qwen27
    python3 routerlm.py grade --config routerlm.json
"""
import argparse
import base64
import copy
import errno
import hashlib
import hmac
import html
import http.client
import ipaddress
import itertools
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.14"
STRICTNESS = {"cloud-ok": 0, "local-only": 1}
LANES = ("interactive", "batch")
TIMEOUTS = (socket.timeout, TimeoutError)  # separate classes before Python 3.10
NEVER = float("inf")

# Section 7, one row per kind of failure: (moves to the next target, marks this one down, alert for a person).
POLICY = {
    "network":       (True, True, None),
    "timeout":       (True, True, None),
    "server_error":  (True, True, None),
    "rate_limited":  (True, True, None),
    "key_rejected":  (True, True, "API key rejected"),
    "out_of_credit": (True, True, "account out of credit"),
    "wrong_model":   (True, True, "local AI app reports a different model"),
    "wrong_weights": (True, True, "model file changed"),
    "busy":          (True, False, None),   # queue full, or no free slot before the first-token timeout
    "bad_request":   (False, False, None),  # the next model would reject it too; the caller gets it as is
}
WEIGHTS_UNKNOWN = "can't check the model file"
KEY_MISSING = "API key missing"  # a target whose key can't be found is out of use, like a rejected key, not a refused start
# Kinds the local health check may clear; the rest wait out the cooldown.
HEALTH_CLEARS = ("network", "health", "wrong_model", "wrong_weights", "weights_unknown", "weights_unchecked")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def password_record(password, iterations=200_000):
    """How the admin password is kept in the config: a salted, slow hash (PBKDF2-SHA256), never the password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"pbkdf2_sha256": digest.hex(), "salt": salt.hex(), "iterations": iterations}


def password_matches(password, rec):
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(rec["salt"]),
                                     int(rec["iterations"]))
        return hmac.compare_digest(digest.hex(), rec["pbkdf2_sha256"])
    except (KeyError, TypeError, ValueError):
        return False


class TargetError(Exception):
    """A target didn't answer. kind is a POLICY key; status and body carry a provider's error for pass-through."""

    def __init__(self, kind, message, status=502, body=None):
        super().__init__(message)
        self.kind, self.message, self.status, self.body = kind, message, status, body


class Refuse(Exception):
    """An error RouterLM returns to the caller. body, when set, is a target's own error passed through."""

    def __init__(self, status, message, code, target=None, body=None, headers=None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code
        self.target, self.body, self.headers = target, body, headers or {}


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------
# Targets, roles, config (sections 4, 5, 6, 15)

class Gate:
    """max_parallel slots on one target, two lanes: interactive first, batch only when nobody waits (section 11)."""

    def __init__(self, slots, depth):
        self.cv = threading.Condition()
        self.free, self.depth = slots, depth
        self.waiting = {"interactive": 0, "batch": 0}

    def _can(self, lane):
        return self.free > 0 and (lane == "interactive" or self.waiting["interactive"] == 0)

    def acquire(self, lane, deadline):
        with self.cv:
            if not self._can(lane) and sum(self.waiting.values()) >= self.depth:
                raise TargetError("busy", "queue full")
            self.waiting[lane] += 1
            try:
                while not self._can(lane):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TargetError("busy", "no free slot before the first-token timeout")
                    self.cv.wait(left)
                self.free -= 1
            finally:
                self.waiting[lane] -= 1

    def release(self):
        with self.cv:
            self.free += 1
            self.cv.notify_all()


class Target:
    """One model on one engine. Holds its own health: down until a time, and why."""

    def __init__(self, d, key, key_problem=None):
        self.source = (d, key, key_problem)  # what it was built from; an unchanged target survives a settings save
        self.id, self.location, self.adapter, self.model = d["id"], d["location"], d["adapter"], d["model"]
        self.base_url = d["base_url"].rstrip("/")
        u = urllib.parse.urlsplit(self.base_url)
        self.scheme, self.host, self.path = u.scheme, u.hostname, u.path
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.key = key
        self.max_tokens = int(d.get("max_tokens", 32000))
        local = self.location == "local"
        self.gate = Gate(int(d.get("max_parallel", 2 if local else 8)), int(d.get("max_queue", 16)))
        self.lock = threading.Lock()
        self.down_until, self.down_kind, self.down_msg = 0.0, None, None
        # Section 6: a pinned model stays out of use until the health check has hashed its file.
        self.weights_sha256, self.weights_path = d.get("weights_sha256"), d.get("weights_path")
        self.weights_seen = None  # (file identity, sha256) from the last hash, reused while the file is unchanged
        if self.weights_sha256:
            self.mark_down("weights_unchecked", f"{self.id}: checking the model file's fingerprint", NEVER)
        self.key_problem = key_problem
        if key_problem:
            self.mark_down("no_key", key_problem, NEVER)
        # For the model's dot on the main page (Rene 10/15): when it last answered, when and why it last failed a
        # try (epoch seconds), and for a local model whether its local AI app passed the last health check.
        self.last_answered, self.last_failed, self.ready = None, None, False

    def answered(self, when=None):
        with self.lock:
            when = when or time.time()
            if self.last_answered is None or when > self.last_answered:
                self.last_answered = when

    def failed(self, why, when=None):
        """Every failed try, not only the last before an answer. A log line read back at start has no reason."""
        with self.lock:
            when = when or time.time()
            if self.last_failed is None or when > self.last_failed[0]:
                self.last_failed = (when, why)

    def is_down(self):
        with self.lock:
            return time.monotonic() < self.down_until

    def cooling(self):
        """Sitting out a cooldown after a failure."""
        with self.lock:
            return time.monotonic() < self.down_until != NEVER

    def testable(self):
        """Held out by a check that a real request tests too: its local AI app not answering or listing another
        model ("test when testable", Rene 10/15), or its model file replaced or not checked yet ("try it anyway",
        Rene 10/15). Not a model with no API key ("no api is not testable")."""
        with self.lock:
            return time.monotonic() < self.down_until == NEVER and self.down_kind != "no_key"

    def mark_down(self, kind, message, seconds):
        with self.lock:
            self.down_until = time.monotonic() + seconds
            self.down_kind, self.down_msg = kind, message

    def mark_up(self, only=None):
        """Clear the down mark; with only, clear it just when it was set for one of those kinds."""
        with self.lock:
            if only is None or self.down_kind in only:
                self.down_until, self.down_kind, self.down_msg = 0.0, None, None

    def state(self):
        """Up or down now, and the model's dot: green when its last try answered (or, for a local model not asked
        yet, its local AI app is ready); red when it's down or its last try failed; grey when it hasn't been asked."""
        with self.lock:
            down = time.monotonic() < self.down_until
            s = {"id": self.id, "location": self.location, "model": self.model, "state": "down" if down else "up"}
            if down:
                s["reason"] = self.down_msg
                if self.down_until != NEVER:
                    s["retry_in_s"] = round(self.down_until - time.monotonic())
            ok, bad = self.last_answered, self.last_failed
            if down or (bad and (ok is None or bad[0] > ok)):
                s["light"] = "red"
            else:
                s["light"] = "green" if ok is not None or self.ready else "grey"
            s["last_answered"] = iso(ok) if ok is not None else None
            s["last_failed"], s["last_failure"] = (iso(bad[0]), bad[1]) if bad else (None, None)
            return s


def is_embed_role(name):
    return name == "embed" or name.startswith("embed-")


class Role:
    def __init__(self, name, targets, lane="interactive", data="cloud-ok", timeout=30.0, max_context=None):
        self.name, self.targets, self.lane, self.data = name, targets, lane, data
        self.timeout, self.max_context = float(timeout), max_context
        self.embeddings = is_embed_role(name)
        self.last_ttft = None  # seconds, for "answers are slow" on the light
        self.last_answer = None  # when the role last answered, from the log


PIN_KINDS = ("wrong_weights", "weights_unknown", "weights_unchecked")  # held out until the model file passes its check
THIS_MAC = "this Mac"  # how an app on the RouterLM machine shows when it sends no name (Rene 10/15)


class Caller:
    def __init__(self, name, d):
        self.name, self.ceiling = name, d.get("data", "cloud-ok")
        self.concrete = bool(d.get("concrete_models", False))


class Config:
    pass


SECRETS_FILE = "secrets.json"  # beside the config, readable by RouterLM's account only; the settings page writes it


def read_secrets(base_dir):
    """The keys entered on the settings page: {name: key}."""
    path = os.path.join(base_dir, SECRETS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            keys = json.load(f).get("keys")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError) as e:
        raise ConfigError(f"can't read {path}: {e}")
    return keys if isinstance(keys, dict) else {}


def write_private(path, text):
    """Write a file only this account can read, replacing the old one in one step."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def resolve_key(ref, secrets=None):
    """api_key is secret:<name> (a key entered on the settings page), keychain:<service>, file:<path> or env:<NAME>.
    Provider keys never sit in the config (section 15)."""
    kind, _, name = ref.partition(":")
    if kind == "secret":
        if not (secrets or {}).get(name):
            raise ConfigError(f"no API key named {name!r} in Settings")
        return secrets[name]
    if kind == "keychain":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", name, "-w"], capture_output=True, text=True)
        except OSError:  # no `security` command: not macOS
            raise ConfigError("keychain: works on macOS only; enter the API key in Settings, or use file: or env:")
        if r.returncode != 0 or not r.stdout.strip():
            raise ConfigError(f"no keychain item named {name!r}")
        return r.stdout.strip()
    if kind == "file":
        try:
            with open(os.path.expanduser(name), encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise ConfigError(f"can't read API key file {name!r}: {e.strerror}")
    if kind == "env":
        if not os.environ.get(name):
            raise ConfigError(f"environment variable {name} is empty")
        return os.environ[name]
    raise ConfigError(f"api_key {ref!r} must start with secret:, keychain:, file: or env:")


def is_private_host(host):
    """True when every address the host resolves to is private, loopback or link-local (section 5)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return bool(infos)


def is_hex64(v):
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def load_config(raw, base_dir=".", resolve_keys=True, secrets=None, previous=None):
    """Check a config dict and build it. Refuses with every problem listed, so the service stops at start (section 15)
    and the settings page can say what's wrong. secrets: the settings page's keys (read from secrets.json when None).
    previous: the running config; a target whose settings didn't change keeps its health, slots and pin."""
    p = []
    if not isinstance(raw, dict):
        raise ConfigError("config refused:\n  - the file must hold one JSON object")

    host, port = None, None
    listen = raw.get("listen")
    if not isinstance(listen, str) or ":" not in listen:
        p.append('listen must be "address:port" on the office network, e.g. "10.0.0.20:8080"')
    else:
        host, _, port_s = listen.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            p.append(f"listen port {port_s!r} isn't a number")
        if host in ("", "0.0.0.0", "::"):
            p.append("listen on the LAN interface's own address, not every interface (section 16)")
        elif not is_private_host(host):
            p.append(f"listen address {host!r} isn't on a private network (section 16)")

    callers = raw.get("apps", {})
    if not isinstance(callers, dict):
        p.append("apps must map each app's name to its key_sha256")
        callers = {}
    for name, c in callers.items():
        if not isinstance(c, dict) or not is_hex64(c.get("key_sha256")):
            p.append(f"app {name!r}: key_sha256 must be 64 lowercase hex characters (routerlm.py new-key makes one)")
        elif c.get("data", "cloud-ok") not in STRICTNESS:
            p.append(f"app {name!r}: data must be cloud-ok or local-only")
    admin = raw.get("admin_password")
    if admin is not None and not (isinstance(admin, dict) and is_hex64(admin.get("pbkdf2_sha256"))
                                  and isinstance(admin.get("salt"), str)
                                  and isinstance(admin.get("iterations"), int) and admin["iterations"] >= 1000):
        p.append("admin_password must be what the settings page writes; remove it to set a new password there")

    targets, keys = {}, {}
    raw_targets = raw.get("models", [])
    if not isinstance(raw_targets, list):
        p.append("models must be a list")
        raw_targets = []
    for d in raw_targets:
        tid = d.get("id") if isinstance(d, dict) else None
        if not isinstance(tid, str) or not tid:
            p.append("every model needs an id")
            continue
        if tid in targets:
            p.append(f"model id {tid!r} is used twice")
            continue
        bad = len(p)
        if d.get("location") not in ("local", "cloud"):
            p.append(f"model {tid!r}: location must be local or cloud")
        if d.get("adapter") not in ADAPTERS:
            p.append(f"model {tid!r}: adapter must be one of {', '.join(ADAPTERS)}")
        if not isinstance(d.get("model"), str) or not d["model"]:
            p.append(f"model {tid!r}: the model's own name (\"model\") is missing")
        u = urllib.parse.urlsplit(d.get("base_url") or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            p.append(f"model {tid!r}: base_url must be an http(s) URL")
        elif d.get("location") == "local" and not is_private_host(u.hostname):
            p.append(f"model {tid!r} is marked local but {u.hostname!r} is outside private address space (section 5)")
        ref = d.get("api_key")
        if d.get("location") == "cloud" and not ref:
            p.append(f"model {tid!r}: a cloud model needs an API key")
        pin, wpath = d.get("weights_sha256"), d.get("weights_path")
        if pin is not None and not is_hex64(pin):
            p.append(f"model {tid!r}: weights_sha256 must be 64 lowercase hex characters (shasum -a 256 <file>)")
        elif pin is not None and (d.get("location") != "local" or d.get("adapter") != "openai"):
            p.append(f"model {tid!r}: weights_sha256 pins the model file of a local AI app (section 6)")
        if wpath is not None and (not isinstance(wpath, str) or not wpath):
            p.append(f"model {tid!r}: weights_path must be the model file's path")
        key, key_problem = None, None
        if ref and resolve_keys:
            try:
                if secrets is None and str(ref).startswith("secret:"):
                    secrets = read_secrets(base_dir)
                key = resolve_key(ref, secrets)
            except ConfigError as e:  # out of use and alerted, so the key can still be added from Settings
                key_problem = f"{tid}: {e}"
        if len(p) == bad:
            if wpath:
                d = dict(d, weights_path=os.path.join(base_dir, os.path.expanduser(wpath)))
            targets[tid] = d
            keys[tid] = (key, key_problem)

    roles = raw.get("lineups", {})
    if not isinstance(roles, dict):
        p.append("lineups must map each lineup's name to its settings")
        roles = {}
    for name, r in roles.items():
        names = r.get("models") if isinstance(r, dict) else None
        if not isinstance(names, list) or not names:
            p.append(f"lineup {name!r}: models must list at least one model")
            continue
        missing = [n for n in names if n not in targets and not any(isinstance(d, dict) and d.get("id") == n for d in raw_targets)]
        for n in missing:
            p.append(f"lineup {name!r} names model {n!r}, which doesn't exist")
        if missing:
            continue
        if any(n not in targets for n in names):
            continue  # the target itself was refused above
        if r.get("lane", "interactive") not in LANES:
            p.append(f"lineup {name!r}: lane must be interactive or batch")
        if r.get("data", "cloud-ok") not in STRICTNESS:
            p.append(f"lineup {name!r}: data must be cloud-ok or local-only")
        if r.get("data") == "local-only":
            for n in names:
                if targets[n]["location"] == "cloud":
                    p.append(f"lineup {name!r} is local only but lists cloud model {n!r}")
        if is_embed_role(name):
            if len(names) != 1:
                p.append(f"lineup {name!r}: an embedding lineup has exactly one model; vectors from different models don't compare (section 9)")
            elif targets[names[0]]["location"] != "local" or targets[names[0]]["adapter"] != "openai":
                p.append(f"lineup {name!r}: the embedding model must be local and use the openai adapter (section 9)")
        t = r.get("timeout_first_token_s", 30)
        if not isinstance(t, (int, float)) or t <= 0:
            p.append(f"lineup {name!r}: timeout_first_token_s must be a positive number")
        mc = r.get("max_context")
        if mc is not None and (not isinstance(mc, int) or mc <= 0):
            p.append(f"lineup {name!r}: max_context must be a positive whole number")
    if p:
        raise ConfigError("config refused:\n  - " + "\n  - ".join(p))

    cfg = Config()
    cfg.host, cfg.port = host, port
    cfg.cooldown_s = float(raw.get("cooldown_s", 60))
    cfg.health_interval_s = float(raw.get("health_interval_s", 5))
    cfg.stream_idle_timeout_s = float(raw.get("stream_idle_timeout_s", 300))
    cfg.log_dir = os.path.join(base_dir, raw.get("log_dir", "logs"))
    cfg.check_dir = os.path.join(base_dir, raw.get("test_questions_dir", "test-questions"))
    cfg.debug = bool(raw.get("debug", False))
    old = previous.targets if previous else {}
    cfg.targets = {tid: old[tid] if tid in old and old[tid].source == (d,) + keys[tid] else Target(d, *keys[tid])
                   for tid, d in targets.items()}
    cfg.by_model = {}
    for t in cfg.targets.values():
        cfg.by_model.setdefault(t.model, t)
    cfg.roles = {
        name: Role(name, [cfg.targets[n] for n in r["models"]], r.get("lane", "interactive"),
                   r.get("data", "cloud-ok"), r.get("timeout_first_token_s", 30), r.get("max_context"))
        for name, r in roles.items()
    }
    cfg.callers = {c["key_sha256"]: Caller(name, c) for name, c in callers.items()}
    cfg.app_names = {name: cfg.callers[c["key_sha256"]] for name, c in callers.items()}
    cfg.admin_password = admin
    cfg.raw, cfg.base_dir, cfg.path = raw, base_dir, None
    return cfg


def read_config(path, resolve_keys=True):
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e.strerror}")
    except ValueError as e:
        raise ConfigError(f"{path} isn't valid JSON: {e}")
    cfg = load_config(raw, os.path.dirname(os.path.abspath(path)), resolve_keys)
    cfg.path = os.path.abspath(path)
    return cfg


# ---------------------------------------------------------------------------------------------------------
# Talking to targets (sections 6, 7, 10)

SSL_CONTEXT = ssl.create_default_context()


class Clock:
    """The first-token deadline for one attempt; once tokens flow, each read waits up to the idle limit instead."""

    def __init__(self, first_token_s, idle_s):
        self.deadline = time.monotonic() + first_token_s
        self.first_token_s, self.idle_s, self.flowing = first_token_s, idle_s, False

    def left(self):
        return self.idle_s if self.flowing else max(0.001, self.deadline - time.monotonic())

    def timed_out(self, t):
        if self.flowing:
            return TargetError("timeout", f"{t.id}: the stream stalled")
        return TargetError("timeout", f"{t.id}: no first token within {self.first_token_s:g}s")


def error_obj(raw):
    try:
        obj = json.loads(raw or b"{}")
    except ValueError:
        return {"message": (raw or b"").decode("utf-8", "replace")[:300]}
    err = obj.get("error") if isinstance(obj, dict) else None
    return err if isinstance(err, dict) else {"message": str(err or "")}


def kind_for_status(status):
    if status in (401, 403):
        return "key_rejected"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "bad_request"


def post(t, path, body, headers, clock, adapter):
    """POST JSON to a target. Returns (conn, sock, resp) for a 2xx; raises TargetError otherwise."""
    if t.scheme == "https":
        conn = http.client.HTTPSConnection(t.host, t.port, timeout=clock.left(), context=SSL_CONTEXT)
    else:
        conn = http.client.HTTPConnection(t.host, t.port, timeout=clock.left())
    try:
        conn.request("POST", t.path + path, json.dumps(body).encode("utf-8"),
                     dict({"Content-Type": "application/json"}, **headers))
        sock = conn.sock  # kept: http.client drops conn.sock for close-delimited responses
        resp = conn.getresponse()
        if resp.status >= 400:
            sock.settimeout(clock.left())
            raw = resp.read(65536)
            conn.close()
            raise adapter.error(t, resp.status, raw)
    except TIMEOUTS:
        conn.close()
        raise clock.timed_out(t)
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise TargetError("network", f"{t.id}: {e.__class__.__name__}: {e}")
    return conn, sock, resp


def read_all(resp, sock, clock, t):
    try:
        sock.settimeout(clock.left())
        return resp.read()
    except TIMEOUTS:
        raise clock.timed_out(t)
    except (OSError, http.client.HTTPException) as e:
        raise TargetError("network", f"{t.id}: {e}")


def sse(resp, sock, clock, t):
    """Yield (event, data) from a server-sent-event stream. Each read waits only as long as the clock allows."""
    event, data = "", []
    while True:
        try:
            sock.settimeout(clock.left())
            line = resp.readline(1 << 22)
        except TIMEOUTS:
            raise clock.timed_out(t)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise TargetError("network", f"{t.id}: {e}")
        if not line:
            if data:
                yield event, "\n".join(data)
            return
        line = line.rstrip(b"\r\n").decode("utf-8", "replace")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)


def completion_to_chunks(obj):
    """An engine that ignored stream=true answered in one piece; turn it into chunks."""
    for c in obj.get("choices") or []:
        m = c.get("message") or {}
        delta = {k: m[k] for k in ("role", "content", "refusal", "tool_calls", "reasoning_content") if m.get(k)}
        for i, tc in enumerate(delta.get("tool_calls") or []):
            tc.setdefault("index", i)
        yield {"id": obj.get("id"), "object": "chat.completion.chunk", "created": obj.get("created"),
               "model": obj.get("model"), "choices": [{"index": c.get("index", 0), "delta": delta,
                                                      "finish_reason": c.get("finish_reason") or "stop"}]}
    if obj.get("usage"):
        yield {"id": obj.get("id"), "object": "chat.completion.chunk", "choices": [], "usage": obj["usage"]}


class OpenAIAdapter:
    """The OpenAI-compatible API: every local engine, and any provider with a compatible endpoint (section 6)."""

    @staticmethod
    def headers(t):
        return {"Authorization": f"Bearer {t.key}"} if t.key else {}

    @staticmethod
    def error(t, status, raw):
        err = error_obj(raw)
        credit = "insufficient_quota" in (str(err.get("code")), str(err.get("type")))
        kind = "out_of_credit" if status == 402 or credit else kind_for_status(status)
        return TargetError(kind, f"{t.id}: HTTP {status} {err.get('message') or ''}".strip(), status, raw)

    def chat(self, t, req, clock):
        body = dict(req, model=t.model, stream=True)
        body["stream_options"] = dict(req.get("stream_options") or {}, include_usage=True)
        conn, sock, resp = post(t, "/chat/completions", body, self.headers(t), clock, self)
        try:
            if "event-stream" not in (resp.getheader("Content-Type") or ""):
                try:
                    yield from completion_to_chunks(json.loads(read_all(resp, sock, clock, t)))
                except ValueError:
                    raise TargetError("server_error", f"{t.id}: the answer wasn't JSON")
                return
            finished = False
            for _, data in sse(resp, sock, clock, t):
                if data.strip() == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    raise TargetError("server_error", f"{t.id}: a stream event wasn't JSON")
                if obj.get("error"):
                    raise TargetError("server_error", f"{t.id}: {error_obj(data.encode()).get('message')}")
                finished = finished or any(c.get("finish_reason") for c in obj.get("choices") or [])
                yield obj
            if not finished:
                raise TargetError("network", f"{t.id}: the stream ended before the answer did")
        finally:
            conn.close()
            resp.close()

    def embed(self, t, req, clock):
        conn, sock, resp = post(t, "/embeddings", dict(req, model=t.model), self.headers(t), clock, self)
        try:
            return json.loads(read_all(resp, sock, clock, t))
        except ValueError:
            raise TargetError("server_error", f"{t.id}: the answer wasn't JSON")
        finally:
            conn.close()


ANTHROPIC_STOP = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "tool_use": "tool_calls",
                  "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "content_filter"}


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def anthropic_blocks(t, content):
    if isinstance(content, str) or content is None:
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                media, _, data = url[5:].partition(";base64,")
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            raise TargetError("bad_request", f"{t.id}: the anthropic adapter can't carry {kind!r} content", 400)
    return blocks


def to_anthropic(t, req):
    """OpenAI chat request to an Anthropic Messages request. Sampling settings and n are left out: current
    Claude models reject temperature/top_p with a 400. Forced tool choice goes as auto for the same reason."""
    system, messages = [], []

    def add(role, blocks):
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for m in req.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            if text_of(m.get("content")):
                system.append(text_of(m.get("content")))
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                          "content": text_of(m.get("content")) or "(empty)"}])
        elif role in ("user", "assistant"):
            blocks = anthropic_blocks(t, m.get("content"))
            for tc in m.get("tool_calls") or [] if role == "assistant" else []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": args})
            add(role, blocks)
    body = {"model": t.model, "max_tokens": req.get("max_completion_tokens") or req.get("max_tokens") or t.max_tokens,
            "messages": messages, "stream": True}
    if system:
        body["system"] = "\n\n".join(system)
    stop = req.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if req.get("tools"):
        body["tools"] = [{"name": f["function"]["name"], "description": f["function"].get("description", ""),
                          "input_schema": f["function"].get("parameters") or {"type": "object", "properties": {}}}
                         for f in req["tools"] if f.get("type") == "function"]
        choice = {"type": "none"} if req.get("tool_choice") == "none" else {"type": "auto"}
        if req.get("parallel_tool_calls") is False and choice["type"] == "auto":
            choice["disable_parallel_tool_use"] = True
        body["tool_choice"] = choice
    return body


class AnthropicAdapter:
    """Anthropic's Messages API, translated to and from the OpenAI shape (section 6)."""

    @staticmethod
    def error(t, status, raw):
        err = error_obj(raw)
        msg = str(err.get("message") or "")
        credit = status == 402 or err.get("type") == "billing_error" or "credit balance" in msg.lower()
        kind = "out_of_credit" if credit else kind_for_status(status)
        # No raw body: callers get RouterLM's OpenAI-shaped error, not Anthropic's.
        return TargetError(kind, f"{t.id}: HTTP {status} {err.get('type') or ''} {msg}".strip(), status)

    def chat(self, t, req, clock):
        headers = {"x-api-key": t.key or "", "anthropic-version": "2023-06-01"}
        conn, sock, resp = post(t, "/messages", to_anthropic(t, req), headers, clock, self)
        cid, created, finish, sent_role = "chatcmpl-routerlm", int(time.time()), None, False
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        tool_index = {}

        def chunk(delta, finish_reason=None):
            nonlocal sent_role
            if not sent_role:
                delta = dict(delta, role="assistant")
                sent_role = True
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": t.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

        try:
            for _, data in sse(resp, sock, clock, t):
                try:
                    ev = json.loads(data)
                except ValueError:
                    raise TargetError("server_error", f"{t.id}: a stream event wasn't JSON")
                kind = ev.get("type")
                if kind == "message_start":
                    msg = ev.get("message") or {}
                    cid = msg.get("id") or cid
                    u = msg.get("usage") or {}
                    usage["prompt_tokens"] = sum(u.get(k) or 0 for k in
                                                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                elif kind == "content_block_start":
                    block = ev.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        i = tool_index[ev.get("index")] = len(tool_index)
                        c = chunk({"tool_calls": [{"index": i, "id": block.get("id"), "type": "function",
                                                   "function": {"name": block.get("name"), "arguments": ""}}]})
                    else:
                        c = chunk({})
                    c["_started"] = True  # any block, thinking included, means the model is answering
                    yield c
                elif kind == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield chunk({"content": d["text"]})
                    elif d.get("type") == "input_json_delta" and ev.get("index") in tool_index:
                        yield chunk({"tool_calls": [{"index": tool_index[ev["index"]],
                                                     "function": {"arguments": d.get("partial_json", "")}}]})
                elif kind == "message_delta":
                    usage["completion_tokens"] = (ev.get("usage") or {}).get("output_tokens", 0)
                    finish = ANTHROPIC_STOP.get((ev.get("delta") or {}).get("stop_reason"), "stop")
                    yield chunk({}, finish)
                elif kind == "message_stop":
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    yield {"id": cid, "object": "chat.completion.chunk", "created": created, "model": t.model,
                           "choices": [], "usage": usage}
                    return
                elif kind == "error":
                    err = ev.get("error") or {}
                    k = "rate_limited" if err.get("type") == "rate_limit_error" else "server_error"
                    raise TargetError(k, f"{t.id}: {err.get('type')}: {err.get('message')}")
            raise TargetError("network", f"{t.id}: the stream ended before the answer did")
        finally:
            conn.close()
            resp.close()


ADAPTERS = {"openai": OpenAIAdapter(), "anthropic": AnthropicAdapter()}


def get_json(t, path, timeout):
    cls = http.client.HTTPSConnection if t.scheme == "https" else http.client.HTTPConnection
    kw = {"context": SSL_CONTEXT} if t.scheme == "https" else {}
    conn = cls(t.host, t.port, timeout=timeout, **kw)
    try:
        conn.request("GET", path, headers=OpenAIAdapter.headers(t))
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return json.loads(resp.read())
    finally:
        conn.close()


def get_models(t, timeout):
    """The model ids a local engine reports, for the health check."""
    return [m.get("id") for m in get_json(t, t.path + "/models", timeout).get("data") or []]


def weights_file(t):
    """The model file behind a local target: what the engine says it loaded (llama-server's /props), else the
    target's weights_path. None when neither says. A relative path from the engine can't be found from here."""
    root = t.path[:-3] if t.path.endswith("/v1") else t.path
    try:
        path = get_json(t, root + "/props", 3).get("model_path")
    except Exception:  # not llama-server, or not answering
        path = None
    if isinstance(path, str) and os.path.isabs(path):
        return path
    return t.weights_path


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 23), b""):
            h.update(block)
    return h.hexdigest()


def file_digest(path, seen=None):
    """(file identity, sha256). A model file is gigabytes, so the hash is reused while path, size, inode and
    modification time stay the same; seen is the previous result."""
    st = os.stat(path)
    ident = (os.path.realpath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    if seen and seen[0] == ident:
        return seen
    return ident, hash_file(path)


def is_token(chunk):
    if chunk.get("_started"):
        return True
    for c in chunk.get("choices") or []:
        d = c.get("delta") or {}
        if c.get("finish_reason") or any(d.get(k) for k in
                                         ("content", "tool_calls", "refusal", "reasoning_content", "reasoning")):
            return True
    return False


def assemble(chunks, t):
    """Chunks to one chat.completion, for callers that didn't ask for a stream."""
    first, choices, usage = None, {}, None
    for ch in chunks:
        first = first or ch
        usage = ch.get("usage") or usage
        for c in ch.get("choices") or []:
            slot = choices.setdefault(c.get("index", 0), {"index": c.get("index", 0), "logprobs": None,
                                                          "finish_reason": None,
                                                          "message": {"role": "assistant", "content": None}})
            msg, d = slot["message"], c.get("delta") or {}
            for k in ("content", "refusal", "reasoning_content"):
                if d.get(k):
                    msg[k] = (msg.get(k) or "") + d[k]
            for tc in d.get("tool_calls") or []:
                calls = msg.setdefault("tool_calls", [])
                i = tc.get("index", 0)
                while len(calls) <= i:
                    calls.append({"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                fn = tc.get("function") or {}
                calls[i]["id"] = tc.get("id") or calls[i]["id"]
                calls[i]["function"]["name"] = fn.get("name") or calls[i]["function"]["name"]
                calls[i]["function"]["arguments"] += fn.get("arguments") or ""
            slot["finish_reason"] = c.get("finish_reason") or slot["finish_reason"]
    first = first or {}
    out = {"id": first.get("id") or "chatcmpl-routerlm", "object": "chat.completion",
           "created": first.get("created") or int(time.time()), "model": t.model,
           "choices": [choices[i] for i in sorted(choices)]}
    if usage:
        out["usage"] = usage
    return out


# ---------------------------------------------------------------------------------------------------------
# The router: roles, data class, failover, health, light, log (sections 3, 5, 7, 11, 12, 14)

class Alerts:
    """Things a person needs to hear about. Kept until the target answers again, then marked cleared."""

    def __init__(self, log):
        self.lock, self.items, self.log = threading.Lock(), {}, log

    def add(self, target, what, message):
        with self.lock:
            a = self.items.get((target.id, what))
            fresh = a is None or a["cleared"] is not None
            if a is None:
                a = self.items[(target.id, what)] = {"model": target.id, "alert": what, "first": now_iso(), "count": 0}
            a.update(last=now_iso(), message=message, cleared=None)
            a["count"] += 1
        if fresh:
            self.log.write({"t": now_iso(), "event": "alert", "model": target.id, "alert": what, "message": message})

    def clear(self, target):
        with self.lock:
            for a in self.items.values():
                if a["model"] == target.id and a["cleared"] is None:
                    a["cleared"] = now_iso()

    def active(self):
        with self.lock:
            return [dict(a) for a in self.items.values() if a["cleared"] is None]


class Log:
    """One JSON line per request, one file per month. Never rotated or deleted (section 14)."""

    def __init__(self, folder, debug):
        self.folder, self.debug_on, self.lock = folder, debug, threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def _append(self, prefix, rec):
        path = os.path.join(self.folder, f"{prefix}-{datetime.now(timezone.utc):%Y-%m}.jsonl")
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock, open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def write(self, rec):
        self._append("routerlm", rec)

    def debug(self, rec):
        if self.debug_on:
            self._append("debug", rec)


class Call:
    """What the log keeps about one request. No prompt text, no response text."""

    def __init__(self, caller, role, data, lane, req):
        self.t, self.t0 = now_iso(), time.monotonic()
        self.caller, self.role, self.data, self.lane, self.req = caller, role, data, lane, req
        self.target, self.failover_from, self.ttft, self.usage, self.outcome = None, None, None, None, "ok"
        self.text = []  # filled only while the debug switch is on

    def record(self):
        u = self.usage or {}
        return {"t": self.t, "app": self.caller, "lineup": self.role, "data": self.data, "lane": self.lane,
                "model": self.target.id if self.target else None,
                "location": self.target.location if self.target else None,
                "in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
                "ttft_ms": round(self.ttft * 1000) if self.ttft is not None else None,
                "total_ms": round((time.monotonic() - self.t0) * 1000),
                "outcome": self.outcome, "failover_from": self.failover_from}


class Answer:
    """A target that has started answering: the chunks read so far, the rest of the stream, its slot."""

    def __init__(self, target, head, rest):
        self.target, self.head, self.rest, self.open = target, head, rest, True

    def chunks(self):
        for ch in self.head:
            ch.pop("_started", None)
            yield ch
        for ch in self.rest:
            ch.pop("_started", None)
            yield ch

    def close(self):
        if self.open:
            self.open = False
            self.rest.close()
            self.target.gate.release()


def estimate_tokens(req):
    """About four bytes a token. An estimate: RouterLM has no tokenizer for every model it fronts."""
    return len(json.dumps([req.get("messages"), req.get("tools")], ensure_ascii=False).encode("utf-8")) // 4


class Router:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = Log(cfg.log_dir, cfg.debug)
        self.alerts = Alerts(self.log)
        self.recent = deque(maxlen=200)
        self.daily = {}  # (local day, caller) -> [requests, tokens in, tokens out], for the admin page
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.started = time.time()
        self.check = None  # the admin page's check-set run: {"target", "thread", "error"}
        self.verified = set()  # sha256 of passwords that matched the current admin_password, so PBKDF2 runs once
        self.key_models = {}  # (provider, sha256 of a key) -> (models, problem), asked once per key
        self.restart_for = None  # a saved setting that only takes effect after a restart
        self.replay()
        self.key_alerts()

    def key_alerts(self, rebuilt=()):
        for t in self.cfg.targets.values():
            if t.key_problem:
                self.alerts.add(t, KEY_MISSING, t.key_problem)
            elif t in rebuilt:
                self.alerts.clear(t)

    # -- settings ------------------------------------------------------------------------------------------

    def password_ok(self, password):
        rec = self.cfg.admin_password
        if not rec or password is None:
            return False
        h = sha256(password)
        if h in self.verified:
            return True
        if password_matches(password, rec):
            self.verified.add(h)
            return True
        return False

    def apply(self, raw, secrets=None, change="settings changed"):
        """Check a new config, write it (and the keys, when given), and switch to it without a restart. Refuses with
        the config's own problem list and leaves everything as it was."""
        cfg, path = self.cfg, self.cfg.path
        if not path:
            raise ConfigError("this RouterLM was started without a config file, so settings can't be saved")
        new = load_config(raw, cfg.base_dir, True, secrets, previous=cfg)
        new.path = path
        if secrets is not None:
            write_private(os.path.join(cfg.base_dir, SECRETS_FILE),
                          json.dumps({"keys": secrets}, indent=2, ensure_ascii=False) + "\n")
        write_private(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        for name, r in new.roles.items():
            if name in cfg.roles:
                r.last_answer, r.last_ttft = cfg.roles[name].last_answer, cfg.roles[name].last_ttft
        if (new.log_dir, new.debug) != (cfg.log_dir, cfg.debug):
            self.log = self.alerts.log = Log(new.log_dir, new.debug)
        if new.admin_password != cfg.admin_password:
            self.verified = set()
        if (new.host, new.port) != (cfg.host, cfg.port):
            self.restart_for = f"the new address {new.host}:{new.port}"
        self.cfg = new
        self.key_alerts([t for tid, t in new.targets.items() if cfg.targets.get(tid) is not t])
        self.log.write({"t": now_iso(), "event": "settings", "change": change})
        return new

    # -- requests ------------------------------------------------------------------------------------------

    def resolve(self, caller, body, headers, embeddings):
        """The role, the effective data class and the lane for one request (sections 3 and 5)."""
        name = body.get("model")
        if not isinstance(name, str) or not name:
            raise Refuse(400, "name a lineup in the model field, e.g. \"answer\"", "missing_model")
        role = self.cfg.roles.get(name)
        if role is None:
            t = self.cfg.by_model.get(name)
            if t is None:
                raise Refuse(404, f"no lineup or model named {name!r}; lineups: {', '.join(self.cfg.roles)}",
                             "model_not_found")
            if not caller.concrete:
                raise Refuse(403, f"this API key names lineups, not models; lineups: {', '.join(self.cfg.roles)}",
                             "concrete_model_not_allowed")
            role = Role(name, [t])  # a named model: that target alone, no failover
            role.embeddings = embeddings
        if role.embeddings != embeddings:
            other = "/v1/embeddings" if role.embeddings else "/v1/chat/completions"
            raise Refuse(400, f"lineup {name!r} answers on {other}", "wrong_endpoint")
        asked = (headers.get("X-RouterLM-Data") or "cloud-ok").strip().lower()
        if asked not in STRICTNESS:
            raise Refuse(400, "X-RouterLM-Data must be cloud-ok or local-only", "bad_header")
        data = max((asked, caller.ceiling, role.data), key=STRICTNESS.get)
        lane = (headers.get("X-RouterLM-Lane") or role.lane).strip().lower()
        if lane not in LANES:
            raise Refuse(400, "X-RouterLM-Lane must be interactive or batch", "bad_header")
        if role.max_context and estimate_tokens(body) > role.max_context:
            raise Refuse(400, f"the request is about {estimate_tokens(body)} tokens; lineup {name!r} takes at most "
                              f"{role.max_context}. RouterLM doesn't truncate.", "context_length_exceeded")
        return role, data, lane

    def fail(self, t, e):
        """Apply section 7's row for this failure to the target."""
        _, marks_down, alert = POLICY[e.kind]
        if marks_down:
            t.mark_down(e.kind, e.message, self.cfg.cooldown_s)
            t.failed(e.message)
        if alert:
            self.alerts.add(t, alert, e.message)

    def mark_up(self, t):
        t.answered()
        if t.down_kind in PIN_KINDS:
            return  # it answered as the last one left; it stays out until its file passes its check
        t.mark_up()
        self.alerts.clear(t)

    def open_chat(self, call, role):
        """Walk the role's list until one target starts answering (section 7). Failover happens only here,
        before the first token reaches the caller (section 10)."""
        local_only = call.data == "local-only"
        tried, busy, last = 0, 0, None

        def candidates():
            """The models that are up, in order. When none of them answered, the held-out ones a question tests, in
            order. Models sitting out a cooldown are tried too, but only when every model was down when the request
            came in (Rene 10/15: "try them anyway"); when one that was up fails now, the rest get their cooldown
            ("give it its 60 seconds before retrying")."""
            seen = set()
            for t in role.targets:
                if not (local_only and t.location != "local") and not t.is_down():
                    seen.add(t.id)
                    yield t
            nothing_was_up = not seen
            for t in role.targets:
                if (not (local_only and t.location != "local") and t.id not in seen
                        and (t.testable() or (nothing_was_up and t.cooling()))):
                    seen.add(t.id)
                    yield t

        for t in candidates():
            tried += 1
            clock = Clock(role.timeout, self.cfg.stream_idle_timeout_s)
            try:
                t.gate.acquire(call.lane, clock.deadline)
            except TargetError as e:
                busy += 1
                last = e
                call.failover_from = t.id
                continue
            rest = ADAPTERS[t.adapter].chat(t, call.req, clock)
            head = []
            try:
                for ch in rest:
                    head.append(ch)
                    if is_token(ch):
                        break
            except TargetError as e:
                t.gate.release()
                if not POLICY[e.kind][0]:
                    raise Refuse(e.status, e.message, e.kind, target=t, body=e.body)
                self.fail(t, e)
                last = e
                call.failover_from = t.id
                continue
            except BaseException:
                rest.close()
                t.gate.release()
                raise
            clock.flowing = True
            call.target, call.ttft = t, time.monotonic() - call.t0
            role.last_ttft = call.ttft
            self.mark_up(t)
            return Answer(t, head, rest)
        if local_only and not any(t.location == "local" and not t.is_down() for t in role.targets):
            raise Refuse(503, f"no local model available in {role.name!r}; nothing was sent outside the building",
                         "no_local_model")
        if tried and busy == tried:
            raise Refuse(429, f"every model in {role.name!r} is busy; retry shortly", "busy",
                         headers={"Retry-After": "5"})
        detail = f" (last: {last.message})" if last else ""
        raise Refuse(503, f"no model in {role.name!r} can answer{detail}", "no_model")

    def embed(self, call, role):
        """One pinned local target, no failover; when it's down, fail fast (section 9)."""
        t = role.targets[0]
        if call.data == "local-only" and t.location != "local":
            raise Refuse(503, "no local model available; nothing was sent outside the building", "no_local_model")
        if t.is_down():
            raise Refuse(503, f"embedding model {t.id!r} is down; queue and retry, don't fall back", "no_model")
        clock = Clock(role.timeout, self.cfg.stream_idle_timeout_s)
        try:
            t.gate.acquire(call.lane, clock.deadline)
        except TargetError as e:
            raise Refuse(429, f"{t.id}: {e.message}; retry shortly", "busy", headers={"Retry-After": "5"})
        try:
            obj = ADAPTERS[t.adapter].embed(t, call.req, clock)
        except TargetError as e:
            if not POLICY[e.kind][0]:
                raise Refuse(e.status, e.message, e.kind, target=t, body=e.body)
            self.fail(t, e)
            raise Refuse(503, f"embedding model {t.id!r} didn't answer: {e.message}", "no_model")
        finally:
            t.gate.release()
        call.target, call.ttft = t, time.monotonic() - call.t0
        call.usage = obj.get("usage")
        self.mark_up(t)
        obj["model"] = t.model
        return t, obj

    def finish(self, call):
        rec = call.record()
        self.note(rec)
        self.log.write(rec)
        if self.cfg.debug:
            self.log.debug({"t": call.t, "app": call.caller, "lineup": call.role,
                            "model": call.target.id if call.target else None,
                            "request": call.req, "response": "".join(call.text)})

    def note(self, rec):
        """What the admin page keeps from a log line: recent activity, per-day counts, when a lineup last answered.
        Lines from before the rename (10/15) say caller, role and target; they're read the same way."""
        try:
            day = (datetime.strptime(rec["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                   .astimezone().strftime("%Y-%m-%d"))
        except (KeyError, TypeError, ValueError):
            return
        for new, old in (("app", "caller"), ("lineup", "role"), ("model", "target")):
            if new not in rec and old in rec:
                rec[new] = rec.pop(old)
        self.recent.append(rec)
        with self.lock:
            c = self.daily.setdefault((day, rec.get("app")), [0, 0, 0])
            c[0] += 1
            c[1] += rec.get("in") or 0
            c[2] += rec.get("out") or 0
        role = self.cfg.roles.get(rec.get("lineup"))
        if role is not None and rec.get("outcome") == "ok":
            role.last_answer = rec["t"]
        t, moved = self.cfg.targets.get(rec.get("model")), self.cfg.targets.get(rec.get("failover_from"))
        if t is not None and rec.get("outcome") == "ok":
            t.answered(epoch(rec["t"]))  # a later try that's already recorded wins
        if moved is not None:
            moved.failed(None, epoch(rec["t"]))

    def replay(self):
        """Read this month's and last month's log at start, so a restart doesn't blank the admin page."""
        today = datetime.now(timezone.utc)
        months = sorted({(today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"), today.strftime("%Y-%m")})
        for m in months:
            try:
                f = open(os.path.join(self.cfg.log_dir, f"routerlm-{m}.jsonl"), encoding="utf-8")
            except OSError:
                continue
            with f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and "event" not in rec:
                        self.note(rec)

    def start_check(self, tid):
        """A test-questions run from the admin page, in the background. One at a time."""
        try:
            cs = load_check_set(self.cfg)
        except ConfigError as e:
            raise Refuse(400, str(e), "test_questions_invalid")
        why = check_refusal(self.cfg, cs, tid)
        if why:
            raise Refuse(400, why, "test_questions_refused")
        with self.lock:
            if self.check and self.check["thread"].is_alive():
                raise Refuse(409, f"the test questions are still running against {self.check['target']}",
                             "test_running")
            state = {"target": tid, "error": None}

            def work():
                try:
                    run_check_set(self.cfg, cs, self.cfg.targets[tid], self.log)
                except Exception as e:  # shown on the admin page; the run file says how far it got
                    traceback.print_exc()
                    state["error"] = f"{e.__class__.__name__}: {e}"

            state["thread"] = threading.Thread(target=work, daemon=True)
            self.check = state
            state["thread"].start()

    # -- health and the light ------------------------------------------------------------------------------

    def check_health(self):
        """Local engines only; nobody's cloud key gets billed for background polling (section 7)."""
        for t in list(self.cfg.targets.values()):
            if t.location != "local" or t.adapter != "openai":
                continue
            try:
                ids = get_models(t, timeout=3)
            except Exception as e:  # refused, timed out, not JSON, not the expected shape
                t.mark_down("health", f"{t.id}: local AI app not answering ({e})", NEVER)
                t.ready = False
                continue
            if t.model not in ids:
                msg = f"{t.id}: the local AI app reports {', '.join(map(str, ids)) or 'no model'}, not {t.model}"
                t.mark_down("wrong_model", msg, NEVER)
                self.alerts.add(t, POLICY["wrong_model"][2], msg)
                continue
            problem = self.check_weights(t) if t.weights_sha256 else None
            if problem:
                kind, msg = problem
                t.mark_down(kind, msg, NEVER)
                self.alerts.add(t, POLICY[kind][2] if kind in POLICY else WEIGHTS_UNKNOWN, msg)
                continue
            t.mark_up(only=HEALTH_CLEARS)
            t.ready = True
            if not t.is_down():
                self.alerts.clear(t)

    @staticmethod
    def check_weights(t):
        """Section 6: the file the engine loaded must hash to weights_sha256. None when it does, else (kind, why).
        Hashing blocks this loop for a few seconds per gigabyte, once per change of the file. While the target is
        held out, open_chat skips it and the next model in each lineup answers; the message says so."""
        path = weights_file(t)
        if not path:
            return "weights_unknown", (f"{t.id}: the local AI app doesn't say which file it loaded; fill in the "
                                       f"model's file location in Settings. The next model in its lineups answers "
                                       f"until then.")
        try:
            t.weights_seen = file_digest(path, t.weights_seen)
        except OSError as e:
            return "weights_unknown", (f"{t.id}: can't read {path}: {e.strerror or e}. The next model in its lineups "
                                       f"answers until then.")
        if t.weights_seen[1] != t.weights_sha256:
            return "wrong_weights", (f"{t.id}: {path} has changed (its fingerprint is now {t.weights_seen[1]}, not "
                                     f"{t.weights_sha256}). The next model in its lineups answers meanwhile. Run the "
                                     f"test questions against it, then put the new fingerprint in Settings.")
        return None

    def health_loop(self):
        while True:
            self.check_health()
            if self.stop_event.wait(self.cfg.health_interval_s):
                return

    def light(self, role):
        """Section 12: green on the first target, yellow on a later one or slow, red when nothing can answer."""
        usable = [t for t in role.targets if role.data != "local-only" or t.location == "local"]
        up = [t for t in usable if not t.is_down()]
        if not up:
            return "red"
        slow = role.last_ttft is not None and role.last_ttft > role.timeout / 2
        return "yellow" if up[0] is not usable[0] or slow else "green"

    def status(self):
        return {"routerlm": VERSION, "debug": self.cfg.debug,
                "lineups": {name: {"light": self.light(r), "last_answer": r.last_answer,
                                   "models": [t.state() for t in r.targets]}
                          for name, r in self.cfg.roles.items()},
                "alerts": self.alerts.active()}


# ---------------------------------------------------------------------------------------------------------
# The check set (section 13)

BINS = ("pass", "safe_miss", "unsafe_miss", "error", "ungraded")
GRADES = {"p": "pass", "s": "safe_miss", "u": "unsafe_miss"}
GRADER_PROMPT = """You grade one answer from an AI model against an answer key the business wrote.

pass: the answer gives every required fact and claims nothing false.
safe_miss: the answer claims nothing false but doesn't give every required fact. It says it doesn't know, asks a clarifying question, or leaves a required fact out.
unsafe_miss: the answer states something false as fact. It contradicts a required fact, or claims something on the must-not-claim list.

A required fact counts as given in any wording. Grade what the answer says, not its style.

Reply with one JSON object and nothing else:
{{"grade": "pass" or "safe_miss" or "unsafe_miss", "reason": "one sentence"}}

Question:
{question}

Required facts:
{facts}

Must not claim:
{claims}

The answer to grade:
<<<
{answer}
>>>"""


def load_check_set(cfg):
    """test-questions/questions.json, checked. Refuses with every problem listed, like the config."""
    path = os.path.join(cfg.check_dir, "questions.json")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ConfigError(f"no test questions at {path} ({e.strerror}); test-questions.example.json shows the shape")
    try:
        cs = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"{path} isn't valid JSON: {e}")
    if not isinstance(cs, dict):
        raise ConfigError(f"test questions refused ({path}):\n  - the file must hold one JSON object")
    p = []
    data = cs.setdefault("data", "local-only")
    if data not in STRICTNESS:
        p.append("data must be local-only or cloud-ok")
    m = cs.get("max_safe_misses")
    if not isinstance(m, int) or isinstance(m, bool) or m < 0:
        p.append("max_safe_misses must be a whole number: how many safe misses a backup may have")
    g = cs.get("grader")
    if g is not None:
        gt = cfg.targets.get(g)
        if gt is None or gt not in question_targets(cfg):
            p.append(f"grader {g!r} must be a chat model in the config")
        elif data == "local-only" and gt.location != "local":
            p.append(f"the test questions are local only, so the grader has to be local too; {g!r} is cloud")
    qs = cs.get("questions")
    if not isinstance(qs, list) or not qs:
        p.append("questions must list at least one question")
        qs = []
    ids = set()
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            p.append(f"question {i} must be an object")
            continue
        qid = q.get("id")
        label = f"question {qid!r}" if isinstance(qid, str) and qid else f"question {i}"
        if not isinstance(qid, str) or not qid:
            p.append(f"{label}: needs an id")
        elif qid in ids:
            p.append(f"{label}: id used twice")
        ids.add(qid)
        if not (isinstance(q.get("ask"), str) and q["ask"].strip()) and not (
                isinstance(q.get("messages"), list) and q["messages"]):
            p.append(f"{label}: needs ask (the question) or messages (what an app would send)")
        for k in ("must_contain", "must_not_claim"):
            v = q.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                p.append(f"{label}: {k} must be a list of strings")
        if not q.get("must_contain") and not q.get("must_not_claim"):
            p.append(f"{label}: give must_contain, must_not_claim or both, so there's something to grade against")
    if p:
        raise ConfigError(f"test questions refused ({path}):\n  - " + "\n  - ".join(p))
    cs["_sha256"] = hashlib.sha256(raw).hexdigest()
    return cs


def question_targets(cfg):
    """Targets the check set can ask: all of them except those that only serve embedding roles."""
    embed = {t.id for r in cfg.roles.values() if r.embeddings for t in r.targets}
    chat = {t.id for r in cfg.roles.values() if not r.embeddings for t in r.targets}
    return [t for t in cfg.targets.values() if t.id not in embed or t.id in chat]


def check_refusal(cfg, cs, tid):
    t = cfg.targets.get(tid)
    if t is None:
        return f"no model named {tid!r}"
    if t not in question_targets(cfg):
        return f"{tid} serves embeddings; test questions need a chat model"
    if cs["data"] == "local-only" and t.location != "local":
        return (f"the test questions are local only and {tid} is cloud. Set \"data\": \"cloud-ok\" in "
                f"questions.json if they may leave the building.")
    return None


def question_messages(cs, q):
    if q.get("messages"):
        return q["messages"]
    msgs = [{"role": "system", "content": cs["system"]}] if cs.get("system") else []
    user = f"{q['context']}\n\n{q['ask']}" if q.get("context") else q["ask"]
    return msgs + [{"role": "user", "content": user}]


def question_text(q):
    if q.get("ask"):
        return q["ask"]
    users = [m for m in q.get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
    return text_of(users[-1].get("content")) if users else ""


def ask_one(t, req, first_token_s, idle_s):
    """One request to one target, no failover: the assembled answer and the time to its first token."""
    clock = Clock(first_token_s, idle_s)
    t0 = time.monotonic()
    rest = ADAPTERS[t.adapter].chat(t, req, clock)
    head = []
    try:
        for ch in rest:
            head.append(ch)
            if is_token(ch):
                break
        ttft = time.monotonic() - t0
        clock.flowing = True
        out = assemble(itertools.chain(head, rest), t)
    finally:
        rest.close()
    msg = (out["choices"] or [{}])[0].get("message") or {}
    return msg.get("content") or msg.get("refusal") or "", ttft


def bullets(items):
    return "\n".join(f"- {x}" for x in items or []) or "(none)"


def grade_answer(cfg, grader, q, answer):
    """A stronger model's grade and reason. "ungraded" when it doesn't give one; a person grades those."""
    prompt = GRADER_PROMPT.format(question=question_text(q), facts=bullets(q.get("must_contain")),
                                  claims=bullets(q.get("must_not_claim")), answer=answer)
    try:
        text, _ = ask_one(grader, {"messages": [{"role": "user", "content": prompt}]}, 120,
                          cfg.stream_idle_timeout_s)
    except TargetError as e:
        return "ungraded", f"the grader didn't answer: {e.message}"
    try:
        obj = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        obj = None
    grade = obj.get("grade") if isinstance(obj, dict) else None
    if grade not in ("pass", "safe_miss", "unsafe_miss"):
        return "ungraded", f"the grader's reply wasn't a grade: {text[:200]}"
    return grade, str(obj.get("reason") or "")


def weights_record(t):
    """Which file a run tested and its hash, so a passing run says what to pin."""
    if t.location != "local":
        return None
    path = weights_file(t)
    rec = {"file": path, "sha256": None, "pinned": t.weights_sha256}
    if path:
        try:
            t.weights_seen = file_digest(path, t.weights_seen)
            rec["sha256"] = t.weights_seen[1]
        except OSError as e:
            rec["error"] = f"can't read it: {e.strerror or e}"
    return rec


def save_run(path, run):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run_check_set(cfg, cs, t, log=None):
    """Ask every question to one pinned target, one at a time, no failover (section 13). Grade each answer when
    the check set names a grader. The run file is rewritten after every question, so progress shows."""
    started = datetime.now(timezone.utc)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{t.id}"
    folder = os.path.join(cfg.check_dir, "runs")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, run_id + ".json")
    timeout = max([r.timeout for r in cfg.roles.values() if t in r.targets and not r.embeddings] or [60.0])
    grader = cfg.targets.get(cs.get("grader"))
    run = {"run": run_id, "started": now_iso(), "finished": None, "model": t.id, "model_name": t.model,
           "location": t.location, "weights": weights_record(t),
           "test_questions": {"name": cs.get("name"), "sha256": cs["_sha256"], "questions": len(cs["questions"]),
                         "data": cs["data"], "max_safe_misses": cs["max_safe_misses"]},
           "first_token_timeout_s": timeout, "grader": grader.id if grader else None, "reviewed": None,
           "results": []}
    save_run(path, run)
    for q in cs["questions"]:
        res = {"id": q["id"], "answer": None, "ttft_ms": None, "total_ms": None, "error": None,
               "grade": "ungraded", "reason": None, "graded_by": None}
        t0 = time.monotonic()
        try:
            res["answer"], ttft = ask_one(t, {"messages": question_messages(cs, q)}, timeout,
                                          cfg.stream_idle_timeout_s)
            res["ttft_ms"] = round(ttft * 1000)
        except TargetError as e:
            res["error"], res["grade"] = e.message, "error"
        res["total_ms"] = round((time.monotonic() - t0) * 1000)
        if grader and res["error"] is None:
            res["grade"], res["reason"] = grade_answer(cfg, grader, q, res["answer"])
            res["graded_by"] = grader.id if res["grade"] != "ungraded" else None
        run["results"].append(res)
        save_run(path, run)
    run["finished"] = now_iso()
    save_run(path, run)
    if log:
        log.write(dict({"t": now_iso(), "event": "test_questions", "model": t.id, "run": run_id}, **tally(run)))
    return path, run


def tally(run):
    c = dict.fromkeys(BINS, 0)
    for r in run.get("results") or []:
        c[r.get("grade") if r.get("grade") in c else "ungraded"] += 1
    return c


def gates(cfg, run):
    """Section 13's gate for each chat lineup the run's model is in: (lineup, "primary" or "backup", verdict).
    Primary: every question passes. Backup: no unsafe miss, no question unanswered, and no more safe
    misses than the test questions allow."""
    c, n = tally(run), len(run.get("results") or [])
    finished = run.get("finished") and n == (run.get("test_questions") or {}).get("questions")
    allowed = (run.get("test_questions") or {}).get("max_safe_misses", 0)
    out = []
    for name, role in cfg.roles.items():
        ids = [t.id for t in role.targets]
        if role.embeddings or run.get("model") not in ids:
            continue
        first = ids[0] == run["model"]
        if not finished:
            verdict = "run didn't finish"
        elif c["ungraded"]:
            verdict = "not graded yet"
        elif first:
            verdict = "passes" if c["pass"] == n else "fails"
        else:
            verdict = "passes" if not c["unsafe_miss"] and not c["error"] and c["safe_miss"] <= allowed else "fails"
        out.append((name, "primary" if first else "backup", verdict))
    return out


def run_files(cfg):
    folder = os.path.join(cfg.check_dir, "runs")
    try:
        return [os.path.join(folder, n) for n in sorted(os.listdir(folder), reverse=True) if n.endswith(".json")]
    except OSError:
        return []


def latest_runs(cfg):
    """The newest run per target. Run files are named <UTC time>-<target id>.json."""
    out = {}
    for path in run_files(cfg):
        tid = os.path.basename(path)[17:-5]
        if tid in cfg.targets and tid not in out:
            try:
                with open(path, encoding="utf-8") as f:
                    out[tid] = json.load(f)
            except (OSError, ValueError):
                continue
    return out


def weights_note(w):
    if not w:
        return ""
    if not w.get("sha256"):
        return f"model file {w.get('file') or 'unknown'}{': ' + w['error'] if w.get('error') else ''}"
    if w.get("pinned") == w["sha256"]:
        return f"model file {w['file']}, matches its fingerprint"
    if w.get("pinned"):
        return (f"model file {w['file']} doesn't match the fingerprint in Settings; if this run passes, put "
                f"{w['sha256']} in the model's File fingerprint")
    return (f"model file {w['file']}; to lock the model to this file, put {w['sha256']} in the model's "
            f"File fingerprint")


def run_summary(cfg, run):
    c = tally(run)
    lines = [f"{run['model']} ({run['model_name']}), run {run['run']}",
             f"  pass {c['pass']} · safe miss {c['safe_miss']} · unsafe miss {c['unsafe_miss']} · "
             f"no answer {c['error']} · ungraded {c['ungraded']}"]
    lines += [f"  {name} ({pos}): {verdict}" for name, pos, verdict in gates(cfg, run)]
    if run.get("weights"):
        lines.append("  " + weights_note(run["weights"]))
    return "\n".join(lines)


def grade_cli(cfg, path, ask=input, say=print):
    """A person reviews a run's grades, one answer at a time (section 13: a person grades, or reviews a model's
    grading). Enter keeps the grade shown; q stops and saves what's done."""
    with open(path, encoding="utf-8") as f:
        run = json.load(f)
    try:
        qs = {q["id"]: q for q in load_check_set(cfg)["questions"]}
    except ConfigError:
        qs = {}
    todo = [r for r in run["results"] if not r.get("error")]
    say(f"Run {run['run']}: {run['model']} ({run['model_name']}), {len(todo)} answers to review.\n"
        f"p = pass, s = safe miss, u = unsafe miss, Enter keeps the grade shown, q stops and saves.")
    for i, r in enumerate(todo, 1):
        q = qs.get(r["id"], {})
        say(f"\n[{i}/{len(todo)}] {r['id']}: {question_text(q) or '(question no longer in the test questions)'}")
        say(f"  must contain:   {'; '.join(q.get('must_contain') or []) or '-'}")
        say(f"  must not claim: {'; '.join(q.get('must_not_claim') or []) or '-'}")
        say("  answer:\n    " + (r.get("answer") or "").replace("\n", "\n    "))
        by = f" by {r['graded_by']}" if r.get("graded_by") else ""
        say(f"  grade now: {r['grade']}{by}{': ' + r['reason'] if r.get('reason') else ''}")
        while True:
            a = ask("  grade [p/s/u/Enter/q]: ").strip().lower()
            if a in GRADES or a == "q" or (a == "" and r["grade"] != "ungraded"):
                break
        if a == "q":
            break
        if a and GRADES[a] != r["grade"]:
            r["grade"], r["graded_by"], r["reason"] = GRADES[a], "person", None
        r["reviewed"] = True
    if todo and all(r.get("reviewed") for r in todo):
        run["reviewed"] = now_iso()
    save_run(path, run)
    say("\n" + run_summary(cfg, run))
    return run


# ---------------------------------------------------------------------------------------------------------
# The admin page (sections 3, 12, 14; APPLIANCE.md "Admin page")

def box_facts(folder):
    """Disk free, memory in use, uptime. Best effort: a fact that can't be read is left out."""
    facts = {}
    try:
        du = shutil.disk_usage(folder)
        facts["disk"] = (du.free, du.total)
    except OSError:
        pass
    try:
        if sys.platform == "darwin":
            def sysctl(name):
                return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2).stdout
            facts["box_up_s"] = time.time() - int(sysctl("kern.boottime").split("sec = ")[1].split(",")[0])
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
            page = int(out.split("page size of ")[1].split()[0])
            n = {}
            for line in out.splitlines()[1:]:
                k, _, v = line.partition(":")
                if v.strip().rstrip(".").isdigit():
                    n[k.strip().strip('"')] = int(v.strip().rstrip("."))
            # About what Activity Monitor calls Memory Used: app memory, wired, compressed.
            app = n.get("Anonymous pages", n.get("Pages active", 0)) - n.get("Pages purgeable", 0)
            used = (app + n["Pages wired down"] + n.get("Pages occupied by compressor", 0)) * page
            facts["memory"] = (used, int(sysctl("hw.memsize")))
        else:
            with open("/proc/uptime") as f:
                facts["box_up_s"] = float(f.read().split()[0])
            with open("/proc/meminfo") as f:
                m = {k: int(v.split()[0]) * 1024 for k, _, v in (line.partition(":") for line in f)}
            facts["memory"] = (m["MemTotal"] - m["MemAvailable"], m["MemTotal"])
    except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
        pass
    return facts


def esc(v):
    return html.escape("" if v is None else str(v))


def size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000


def span(s):
    s = int(s)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} days"


def local(iso, fmt="%H:%M:%S"):
    """A log time (UTC) as the box's local time, with how long ago."""
    if not iso:
        return "—"
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return esc(iso)
    ago = (datetime.now(timezone.utc) - t).total_seconds()
    return f"{t.astimezone().strftime(fmt)} <span class=muted>({span(max(ago, 0))} ago)</span>"


def table(head, rows, empty):
    if not rows:
        return f"<p class=muted>{esc(empty)}</p>"
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


ADMIN_CSS = """
:root{--bg:#fbfbf9;--fg:#1d1f1e;--muted:#6b706d;--line:#dfe2df;--card:#fff;--green:#2e9d4f;--yellow:#c99400;
--red:#cf3a2c;--accent:#2d5f8a}
@media (prefers-color-scheme:dark){:root{--bg:#141615;--fg:#e6e8e6;--muted:#9aa09c;--line:#2c302e;--card:#1b1e1c;
--accent:#7fb0d9}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:16px}
main{max-width:1100px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}h1 small{font-weight:400;color:var(--muted)}
h2{font-size:15px;margin:22px 0 8px}.muted{color:var(--muted)}section{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:4px 12px 10px;margin-top:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);
vertical-align:top}th{font-weight:600;color:var(--muted);font-size:12px}td.n{text-align:right}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}.green{background:var(--green)}
.grey{background:var(--muted)}
.yellow{background:var(--yellow)}.red{background:var(--red)}.bad{color:var(--red);font-weight:600}
.banner{border:1px solid var(--red);color:var(--red);border-radius:8px;padding:8px 12px;margin-top:12px}
button{font:inherit;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);
color:var(--accent);cursor:pointer}code{font-size:12px}
"""


def try_it(router, f):
    """One question through a role, the way a caller's request goes (failover, local-only, the log), for the admin
    page's Try it box. Returns what to show."""
    role = router.cfg.roles.get(f.get("role") or "")
    text = (f.get("question") or "").strip()
    if role is None or role.embeddings:
        return {"error": "pick a lineup"}
    if not text:
        return {"error": "type a question"}
    data = "local-only" if f.get("local_only") or role.data == "local-only" else "cloud-ok"
    call = Call("try-it", role.name, data, role.lane, {"model": role.name,
                                                       "messages": [{"role": "user", "content": text}]})
    try:
        ans = router.open_chat(call, role)
    except Refuse as r:
        call.outcome, call.target = r.code, r.target
        router.finish(call)
        return {"error": r.message, "failover_from": call.failover_from}
    out, broke = None, None
    try:
        out = assemble(ans.chunks(), ans.target)
    except TargetError as e:
        call.outcome, broke = "stream_broken", e.message
    finally:
        ans.close()
    call.usage = (out or {}).get("usage")
    router.finish(call)
    if broke:
        return {"error": f"{broke}; the answer broke off"}
    msg = (out["choices"] or [{}])[0].get("message") or {}
    return {"answer": msg.get("content") or msg.get("refusal") or "(no text)", "target": ans.target,
            "ttft": call.ttft, "total": time.monotonic() - call.t0, "failover_from": call.failover_from}


def try_section(router, tried=None, posted=None):
    posted, tried = posted or {}, tried or {}
    names = [n for n, r in router.cfg.roles.items() if not r.embeddings]
    if not names:
        return ""
    box = ("<form method=post action=/try><div class=grid>"
           + choose("Lineup", "role", [(n, n) for n in names], posted.get("role") or names[0])
           + "<label class=check><input type=checkbox name=local_only value=1"
           + (" checked" if posted.get("local_only") else "") + "> Local only: local models answer, nothing goes to "
           "the cloud</label></div>"
           + f"<label>Question<textarea name=question rows=3 style='min-height:0'>{esc(posted.get('question', ''))}"
           "</textarea></label><div class=row><button>Send</button></div></form>")
    if tried.get("error"):
        box += f"<div class='banner tried'>{esc(tried['error'])}</div>"
    elif tried:
        t = tried["target"]
        moved = router.cfg.targets.get(tried.get("failover_from"))
        after = f" · moved on from {esc(model_label(moved))}" if moved else ""
        box += (f"<div class='card tried'><p class=muted>Answered by <b>{esc(model_label(t))}</b> · the answer "
                f"started after {tried['ttft']:.1f} s, done after {tried['total']:.1f} s{after}</p>"
                f"<div style='white-space:pre-wrap'>{esc(tried['answer'])}</div></div>")
    return "<section><h2>Try it</h2>" + box + "</section>"


def admin_page(router, tried=None, posted=None):
    cfg, now = router.cfg, time.time()
    facts = box_facts(cfg.log_dir)
    out = [f"<div class=top><h1>RouterLM <small>{VERSION} · {esc(socket.gethostname())} · {esc(cfg.host)}:{cfg.port}"
           f"</small></h1><a class=gear href=/settings>{GEAR} Settings</a></div>"]
    box = [f"RouterLM running for {span(now - router.started)}"]
    if "box_up_s" in facts:
        box.append(f"this machine on for {span(facts['box_up_s'])}")
    if "memory" in facts:
        box.append(f"memory in use {size(facts['memory'][0])} of {size(facts['memory'][1])}")
    if "disk" in facts:
        box.append(f"disk free {size(facts['disk'][0])} of {size(facts['disk'][1])}")
    out.append(f"<p class=muted>{' · '.join(box)}</p>")
    if not cfg.roles:
        out.append("<div class=ok>Not set up yet. Open <a href=/settings>Settings</a> to add API keys, models, "
                   "lineups and the apps that will send questions to RouterLM.</div>")
    if cfg.debug:
        out.append(f"<div class=banner>Troubleshooting capture is on: every question and answer is saved in full, in "
                   f"<code>{esc(cfg.log_dir)}</code>. Turn it off in <a href=/settings>Settings</a>, under General, "
                   f"when you're done.</div>")

    def named(tid):
        t = cfg.targets.get(tid)
        return esc(model_label(t)) if t else esc(tid or "")

    alerts = [[named(a["model"]), f"<span class=bad>{esc(a['alert'])}</span>",
               esc(without_id(a["model"], a["message"])),
               local(a["first"]), local(a["last"]), str(a["count"])] for a in router.alerts.active()]
    out.append("<section><h2>Alerts</h2>" + table(["Model", "Problem", "Detail", "First seen", "Last seen", "Times"],
                                                   alerts, "None. Everything is answering.") + "</section>")

    roles = []
    for name, r in cfg.roles.items():
        light = router.light(r)
        targets = []
        for i, t in enumerate(r.targets, 1):
            s = t.state()
            if s["state"] == "down":
                status = (f"<span class=bad>down</span> "
                          f"<span class=muted>{esc(without_id(t.id, s.get('reason')))}</span>")
            elif s["light"] == "red":
                status = f"<span class=bad>failed {local(s['last_failed'])}</span>" + (
                    f" <span class=muted>{esc(without_id(t.id, s['last_failure']))}</span>"
                    if s["last_failure"] else "")
            elif s["last_answered"]:
                status = f"<span class=muted>answered {local(s['last_answered'])}</span>"
            else:
                status = f"<span class=muted>{'ready, ' if s['light'] == 'green' else ''}not asked yet</span>"
            targets.append(f"<span class='dot {s['light']}'></span><b>#{i}{' Primary' if i == 1 else ''}</b> "
                           f"{esc(model_label(t))} · {status}")
        roles.append([f"<span class='dot {light}'></span>{light}", f"<b>{esc(name)}</b>", local(r.last_answer),
                      "<br>".join(targets)])
    out.append("<section><h2>Lineups</h2><p class=muted>#1 is the Primary and answers while it can. When it can't, "
               "#2 answers, and so on down. The lineup's light: green, the Primary is answering; yellow, a backup is "
               "answering or answers are slow; red, nothing in the lineup can answer. Each model's dot: green, its "
               "last try answered; red, it's down or its last try failed; grey, it hasn't been asked yet. Change the "
               "order in <a href=/settings>Settings</a>.</p>"
               + table(["Light", "Lineup", "Last answered", "Models, in order"], roles, "No lineups.")
               + "</section>")
    out.append(try_section(router, tried, posted))

    recent = []
    for rec in list(router.recent)[::-1][:25]:
        ttft = f"{rec['ttft_ms'] / 1000:.1f} s" if rec.get("ttft_ms") is not None else "—"
        outcome = esc(OUTCOMES.get(rec.get("outcome"), rec.get("outcome")))
        if rec.get("outcome") != "ok":
            outcome = f"<span class=bad>{outcome}</span>"
        recent.append([local(rec.get("t")), esc(rec.get("app")), esc(rec.get("lineup")),
                       named(rec.get("model")) or "—", outcome, ttft,
                       f"{(rec.get('total_ms') or 0) / 1000:.1f} s", named(rec.get("failover_from"))])
    out.append("<section><h2>Recent questions</h2>" + table(
        ["Time", "App", "Lineup", "Answered by", "Result", "Answer started after", "Done after", "Moved on from"],
        recent, "No questions yet.") + "</section>")

    with router.lock:
        daily = dict(router.daily)
    days = sorted({d for d, _ in daily}, reverse=True)[:7]
    usage = [[esc(d), esc(c), f"{daily[(d, c)][0]:,}", f"{daily[(d, c)][1]:,}", f"{daily[(d, c)][2]:,}"]
             for d in days for c in sorted({c for dd, c in daily if dd == d}, key=str)]
    out.append("<section><h2>Questions and tokens per day</h2><p class=muted>Tokens are how model companies count "
               "and bill text: about 4 characters each. In: the question. Out: the answer.</p>" + table(
        ["Day", "App", "Questions", "Tokens in", "Tokens out"], usage, "Nothing logged in the last two months.")
               + "</section>")

    out.append("<section><h2>Test questions</h2>" + check_section(router) + "</section>")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>RouterLM admin</title>"
            f"<style>{ADMIN_CSS}{SETTINGS_CSS}</style></head><body><main>{''.join(out)}</main>"
            "<script>setInterval(function(){var q=document.querySelector('textarea[name=question]');"
            "if(document.querySelector('.tried')||(q&&(q.value||document.activeElement===q)))return;"
            "location.reload()},15000)</script></body></html>")


def check_section(router):
    cfg = router.cfg
    path = os.path.join(cfg.check_dir, "questions.json")
    if not os.path.exists(path):
        return (f"<p class=muted>None yet. Test questions are your own questions with the facts each answer must "
                f"contain, asked of a model to see whether it's good enough before you rely on it. Whoever set "
                f"RouterLM up writes them in <code>{esc(path)}</code>; <code>test-questions.example.json</code> "
                f"shows how. They show up here once the file is there.</p>")
    try:
        cs = load_check_set(cfg)
    except ConfigError as e:
        return f"<pre>{esc(e)}</pre>"
    running = router.check if router.check and router.check["thread"].is_alive() else None
    runs = latest_runs(cfg)
    where = "local models only" if cs["data"] == "local-only" else "may go to the cloud"
    grader = cfg.targets.get(cs.get("grader"))
    misses = cs["max_safe_misses"]
    head = (f"<p class=muted>{esc(cs.get('name') or 'questions.json')}: {len(cs['questions'])} questions, "
            f"{where}, graded by {esc(model_label(grader)) if grader else 'a person'}. A backup may say "
            f"\"I don't know\" or leave a fact out {misses} time{'' if misses == 1 else 's'}; the Primary has to get "
            f"every one right.</p>")
    if router.check and router.check.get("error"):
        head += f"<p class=bad>The last run from this page stopped: {esc(router.check['error'])}</p>"
    rows = []
    for t in question_targets(cfg):
        run = runs.get(t.id)
        if run:
            c, n = tally(run), len(run["results"])
            if run.get("finished"):
                when = local(run["finished"], "%Y-%m-%d %H:%M")
            elif running and running["target"] == t.id:
                when = f"running: {n} of {run['test_questions']['questions']}"
            else:
                when = f"<span class=bad>stopped at {n} of {run['test_questions']['questions']}</span>"
            if run["test_questions"]["sha256"] != cs["_sha256"]:
                when += " <span class=muted>(questions changed since)</span>"
            run_grader = cfg.targets.get(run.get("grader"))
            graded = (f"checked by a person {local(run['reviewed'], '%Y-%m-%d')}" if run.get("reviewed") else
                      f"{esc(model_label(run_grader) if run_grader else run['grader'])}, not checked by a person yet"
                      if run.get("grader") else "a person, not done yet")
            unsafe = f"<span class=bad>{c['unsafe_miss']}</span>" if c["unsafe_miss"] else "0"
            counts = (f"{c['pass']} / {c['safe_miss']} / {unsafe}"
                      + (f" · no answer {c['error']}" if c["error"] else "")
                      + (f" · ungraded {c['ungraded']}" if c["ungraded"] else ""))
            gate = "<br>".join(f"{esc(name)}, as {'Primary' if pos == 'primary' else 'a backup'}: "
                               + (v if v == "passes" else f"<span class=bad>{v}</span>")
                               for name, pos, v in gates(cfg, run)) or "<span class=muted>in no lineup yet</span>"
            weights = esc(weights_note(run.get("weights")))
            last = f"{when}<br><span class=muted>{esc(run['model_name'])}</span>"
        else:
            last, graded, counts, gate, weights = "never", "", "", "", ""
        why = check_refusal(cfg, cs, t.id)
        if why:
            why = why.replace(t.id, model_label(t))  # the page shows the model's name, not its short name
            action = f"<span class=muted>{esc(why)}</span>"
        elif running:
            action = "<span class=muted>a run is going</span>"
        else:
            action = (f"<form method=post action=/test-questions><input type=hidden name=model value='{esc(t.id)}'>"
                      f"<button>Run test questions</button></form>")
        rows.append([f"<b>{esc(model_label(t))}</b>", last, counts, graded, gate, weights, action])
    return head + table(["Model", "Last run", "Right / don't know / wrong", "Graded by", "Good enough?",
                         "Model file", ""], rows, "No models to ask.") + (
        "<p class=muted>A run asks every question of one model, one at a time, and doesn't move on to another "
        "model. To check or change the grades, whoever set RouterLM up runs "
        "<code>python3 routerlm.py grade</code> in a terminal.</p>")


# ---------------------------------------------------------------------------------------------------------
# The settings page: RouterLM set up from a browser, no terminal (DECISIONS.md 2026-10-15, Rene)

PRESETS = {  # where a model runs: (label, location, adapter, base_url)
    "anthropic": ("Anthropic", "cloud", "anthropic", "https://api.anthropic.com/v1"),
    "openai": ("OpenAI", "cloud", "openai", "https://api.openai.com/v1"),
    "other-cloud": ("Another cloud company with an OpenAI-compatible API (give its address)", "cloud", "openai", ""),
    "lmstudio": ("LM Studio on this machine", "local", "openai", "http://127.0.0.1:1234/v1"),
    "ollama": ("Ollama on this machine", "local", "openai", "http://127.0.0.1:11434/v1"),
    "llama-server": ("llama-server on this machine", "local", "openai", "http://127.0.0.1:8080/v1"),
    "other-local": ("A local AI app elsewhere on the office network (give its address)", "local", "openai", ""),
}
WHERE = [("cloud-ok", "Cloud and local"), ("local-only", "Local only: nothing goes to the cloud")]
# What the main page says for each outcome in the log, in words the person reading it uses.
OUTCOMES = {"ok": "answered", "stream_broken": "the answer broke off", "app_gone": "the app stopped waiting",
            "caller_gone": "the app stopped waiting", "no_model": "no model could answer",
            "no_target": "no model could answer", "no_local_model": "no local model could answer",
            "no_local_target": "no local model could answer", "busy": "every model busy; the app was told to retry",
            "context_length_exceeded": "the question was too long", "model_not_found": "no lineup by that name",
            "bad_request": "the model refused the request as malformed"}


def model_label(t, key=None):
    """How a model reads on the pages: its own name, then the company or local AI app, cloud or local."""
    model, url, location = ((t.get("model"), t.get("base_url"), t.get("location")) if isinstance(t, dict)
                            else (t.model, t.base_url, t.location))
    place = next((v[0].replace(" on this machine", "") for v in PRESETS.values()
                  if v[3] and v[3] == (url or "").rstrip("/")), None)
    where = [place, "cloud" if location == "cloud" else "local", f"API key {key}" if key else None]
    return f"{model} ({', '.join(w for w in where if w)})"


def without_id(tid, message):
    """A failure as the pages show it: the row already names the model, so the short name in front goes. The log
    and /status keep it."""
    prefix = f"{tid}: "
    return message[len(prefix):] if message and message.startswith(prefix) else message
NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
NAME_RULE = "lowercase letters, digits, dots, dashes and underscores"
GEAR = ('<svg width=16 height=16 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 '
        'stroke-linecap=round stroke-linejoin=round aria-hidden=true><circle cx=12 cy=12 r=3></circle>'
        '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 '
        '0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 '
        '1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1'
        'a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 '
        '1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 '
        '1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"></path></svg>')


def new_password_problem(password, repeat):
    if len(password) < 8:
        return "use at least 8 characters"
    if password != repeat:
        return "the two entries don't match"
    return None


def restart():
    """Start this same program over, in place: a new listen address needs a fresh socket."""
    os.execv(sys.executable, [sys.executable] + sys.argv)


def number(f, key, label, kind=float, low=None, blank=None):
    v = (f.get(key) or "").strip()
    if v == "":
        return blank
    try:
        n = kind(v)
    except ValueError:
        raise ValueError(f"{label} must be a {'whole ' if kind is int else ''}number")
    if low is not None and n < low:
        raise ValueError(f"{label} must be at least {low}")
    return n


KEY_NAMES = (("sk-ant-", "anthropic"), ("sk-proj-", "openai"), ("sk-or-", "openrouter"), ("sk-", "openai"))


def clean_name(text):
    """A typed name made acceptable instead of refused: "My Key" becomes my-key."""
    return re.sub(r"[^a-z0-9._-]+", "-", (text or "").strip().lower()).strip("-._")[:48]


def free_name(base, taken):
    return next(n for n in itertools.chain([base], (f"{base}-{i}" for i in itertools.count(2))) if n not in taken)


NOT_CHAT = ("embed", "tts", "whisper", "dall-e", "moderation", "audio", "realtime", "transcribe", "image", "search")


def key_provider(value):
    """anthropic or openai, from how a key starts; None when it could be anyone's."""
    if value.startswith("sk-ant-"):
        return "anthropic"
    if value.startswith("sk-") and not value.startswith("sk-or-"):
        return "openai"
    return None


def provider_models(router, value):
    """(chat models, problem) for a key, asked of its provider once and kept while RouterLM runs. Listing models
    costs nothing, and a rejected key shows up here, before anything depends on it."""
    provider = key_provider(value)
    if not provider:
        return [], None
    cached = router.key_models.get((provider, sha256(value)))
    if cached:
        return cached
    label, _, _, url = PRESETS[provider]
    u = urllib.parse.urlsplit(url)
    headers = ({"x-api-key": value, "anthropic-version": "2023-06-01"} if provider == "anthropic"
               else {"Authorization": f"Bearer {value}"})
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=5, context=SSL_CONTEXT)
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=5)
    try:
        conn.request("GET", u.path + "/models?limit=1000", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
    except Exception as e:  # not cached: the network may come back
        return [], f"couldn't reach {label} to list its models ({e.__class__.__name__})"
    finally:
        conn.close()
    if resp.status in (401, 403):
        result = ([], f"{label} rejected this API key")
    elif resp.status != 200:
        result = ([], f"{label} answered HTTP {resp.status} when asked for its models")
    else:
        try:
            data = [m for m in json.loads(body).get("data") or [] if isinstance(m, dict)]
        except (ValueError, AttributeError):
            data = []
        if provider == "openai":  # unordered there; newest first, as Anthropic lists them
            data.sort(key=lambda m: m.get("created") or 0, reverse=True)
        ids = [m["id"] for m in data if isinstance(m.get("id"), str) and not any(w in m["id"].lower() for w in NOT_CHAT)]
        plain = {i: re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", i) for i in ids}
        # A dated copy (gpt-5.5-2026-04-23) goes when its plain name (gpt-5.5) is listed too.
        result = ([i for i in ids if plain[i] == i or plain[i] not in plain], None)
    router.key_models[(provider, sha256(value))] = result
    return result


def add_target(targets, by_id, t, suffix):
    """Add a target, or reuse the identical one already there; a clash with a different one takes the suffix."""
    if t["id"] in by_id and by_id[t["id"]] != t:
        t["id"] = clean_name(f"{t['id']}-{suffix}")
    if t["id"] not in by_id:
        targets.append(t)
        by_id[t["id"]] = t
    return t["id"]


def local_target(url, model):
    preset = next((k for k, v in PRESETS.items() if v[3] == url), "local")
    return {"id": clean_name(f"{preset.split('-')[0]}-{model}"), "location": "local", "adapter": "openai",
            "base_url": url, "model": model}


def cloud_target(provider, model, key_name):
    _, _, adapter, url = PRESETS[provider]
    return {"id": clean_name(f"{provider}-{model}"), "location": "cloud", "adapter": adapter, "base_url": url,
            "model": model, "api_key": f"secret:{key_name}"}


ENGINES = ("lmstudio", "ollama", "llama-server")


def engine_starter(preset):
    """The command that starts an installed engine's server on its usual port, or None. llama-server has none: it
    needs a model file named when it starts."""
    if preset == "lmstudio":
        lms = os.path.expanduser("~/.lmstudio/bin/lms")
        return [lms, "server", "start", "--port", "1234"] if os.path.exists(lms) else None
    if preset == "ollama":
        exe = shutil.which("ollama") or next((p for p in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama",
                                                          "/Applications/Ollama.app/Contents/Resources/ollama")
                                              if os.path.exists(p)), None)
        return [exe, "serve"] if exe else None
    return None


def lmstudio_loaded(url):
    """The models LM Studio has in memory now (its own /api/v0/models), so the lists can say which are loaded:
    picking an unloaded 20 GB model beside a loaded one makes a 32 GB machine swap."""
    u = urllib.parse.urlsplit(url)
    root = u.path[:-3] if u.path.endswith("/v1") else u.path
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=1)
    try:
        conn.request("GET", root + "/api/v0/models")
        resp = conn.getresponse()
        data = json.loads(resp.read()).get("data") if resp.status == 200 else None
        return {m.get("id") for m in data or [] if isinstance(m, dict) and m.get("state") == "loaded"}
    except Exception:  # an older LM Studio, or another engine on that port
        return set()
    finally:
        conn.close()


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def local_label(url, model, loaded=()):
    engine = next((v[0] for v in PRESETS.values() if v[3] == url), url)
    return f"{model} ({engine}{', loaded' if model in loaded else ''})"


def local_picks(engines):
    return [(u, m) for u, ms in engines.items() for m in ms if "embed" not in str(m).lower()]


def settings_action(router, f):
    """One form from the settings page. Returns (what changed, a new app key to show once or None). Every
    change goes through the config's own checks (router.apply) and is refused whole if they fail."""
    cfg = router.cfg
    raw = copy.deepcopy(cfg.raw)
    targets, roles, callers = raw.setdefault("models", []), raw.setdefault("lineups", {}), raw.setdefault("apps", {})
    do, name = f.get("do", ""), (f.get("name") or "").strip()
    by_id = {t.get("id"): t for t in targets if isinstance(t, dict)}

    if do in ("key_add", "key_replace", "key_delete"):
        keys = dict(read_secrets(cfg.base_dir))
        if do == "key_delete":
            users = [model_label(t) for t in by_id.values() if t.get("api_key") == f"secret:{name}"]
            if users:
                raise ValueError(f"API key {name} is used by {', '.join(users)}; give those models another API key "
                                 f"first")
            keys.pop(name, None)
            router.apply(raw, keys, f"API key deleted: {name}")
            return f"API key {name} deleted.", None
        value = (f.get("value") or "").strip()
        if not value:
            raise ValueError("paste the API key itself")
        if do == "key_add":  # left blank, the name comes from how the key starts
            name = clean_name(name) or free_name(next((n for p, n in KEY_NAMES if value.startswith(p)), "key"), keys)
        if do == "key_add" and name in keys:
            raise ValueError(f"there's already an API key named {name}; use Replace on it")
        keys[name] = value
        router.apply(raw, keys, f"API key {'added' if do == 'key_add' else 'replaced'}: {name}")
        return f"API key {name} saved.", None

    if do == "target_add":
        if f.get("preset") not in PRESETS:
            raise ValueError("choose where the model runs")
        _, location, adapter, url = PRESETS[f["preset"]]
        url, model = (f.get("base_url") or "").strip() or url, (f.get("model") or "").strip()
        if not url:
            raise ValueError("give the address of the local AI app or cloud company, e.g. http://10.0.0.30:8080/v1")
        if not model:
            raise ValueError("name the model the way the local AI app or company lists it")
        t = {"location": location, "adapter": adapter, "base_url": url, "model": model}
        if f.get("key"):
            t["api_key"] = f["key"]
        same = next((x for x in by_id.values() if all(x.get(k) == t.get(k) for k in ("base_url", "model", "api_key"))),
                    None)
        if same:
            raise ValueError(f"{model_label(same)} is already set up")
        # The short name is RouterLM's own, made here; the screens show the model's name and where it runs (Rene).
        tid = free_name(clean_name(f"{f['preset'].split('-')[0]}-{model}") or "model", by_id)
        t = dict({"id": tid}, **t)
        targets.append(t)
        router.apply(raw, change=f"model added: {tid}")
        return f"Model {model_label(t)} added. Put it in a lineup to use it.", None

    if do in ("target_save", "target_delete"):
        t = by_id.get(name)
        if t is None:
            raise ValueError(f"no model named {name}")
        if do == "target_delete":
            using = [r for r, v in roles.items() if name in (v.get("models") or [])]
            if using:
                raise ValueError(f"{model_label(t)} is in {', '.join(using)}; take it out of those lineups first")
            targets.remove(t)
            router.apply(raw, change=f"model deleted: {name}")
            return f"Model {model_label(t)} deleted.", None
        for k in ("model", "base_url", "location", "adapter", "api_key", "weights_sha256", "weights_path"):
            v = (f.get(k) or "").strip()
            if v:
                t[k] = v.lower() if k == "weights_sha256" else v
            else:
                t.pop(k, None)
        mp = number(f, "max_parallel", "Questions it works on at once", int, 1)
        t.pop("max_parallel", None) if mp is None else t.update(max_parallel=mp)
        router.apply(raw, change=f"model changed: {name}")
        return f"Model {model_label(t)} saved.", None

    if do in ("role_add", "role_save", "role_delete"):
        if do == "role_add":
            name = clean_name(name) or free_name("answer", roles)
            if name in roles:
                raise ValueError(f"there's already a lineup named {name}")
            roles[name] = {}
        if name not in roles:
            raise ValueError(f"no lineup named {name}")
        if do == "role_delete":
            del roles[name]
            router.apply(raw, change=f"lineup deleted: {name}")
            return f"Lineup {name} deleted.", None
        r = roles[name]
        order = []
        for _, tid in sorted((int(k[1:]), v) for k, v in f.items() if re.fullmatch(r"t\d+", k)):
            if tid.startswith("new:"):  # a key's model, not set up yet
                kname, _, model = tid[4:].partition("|")
                provider = key_provider(read_secrets(cfg.base_dir).get(kname) or "")
                if not provider or not model:
                    raise ValueError(f"no API key named {kname} for {model}")
                tid = add_target(targets, by_id, cloud_target(provider, model, kname), kname)
            elif tid.startswith("new-local:"):  # a model this machine's engine runs, not set up yet
                url, _, model = tid[10:].partition("|")
                tid = add_target(targets, by_id, local_target(url, model), "local")
            if tid and tid not in order:
                order.append(tid)
        r["models"] = order
        for k, choices in (("lane", LANES), ("data", tuple(STRICTNESS))):
            if f.get(k) in choices:
                r[k] = f[k]
        t = number(f, "timeout_first_token_s", "Move on if no answer starts within", float, 0.1)
        mc = number(f, "max_context", "Longest question", int, 1)
        r.pop("timeout_first_token_s", None) if t is None else r.update(timeout_first_token_s=t)
        r.pop("max_context", None) if mc is None else r.update(max_context=mc)
        router.apply(raw, change=f"lineup {'added' if do == 'role_add' else 'changed'}: {name}")
        return f"Lineup {name} saved.", None

    if do in ("caller_add", "caller_save", "caller_key", "caller_delete"):
        if do == "caller_add":
            name = clean_name(name) or free_name("my-app", callers)
            if name in callers:
                raise ValueError(f"there's already an app named {name}")
            callers[name] = {}
        if name not in callers:
            raise ValueError(f"no app named {name}")
        if do == "caller_delete":
            del callers[name]
            router.apply(raw, change=f"app deleted: {name}")
            return f"App {name} deleted. Its API key no longer works from other computers.", None
        c, key = callers[name], None
        if do in ("caller_add", "caller_key"):
            key = "rlm_" + secrets.token_urlsafe(32)
            c["key_sha256"] = sha256(key)
        if do in ("caller_add", "caller_save"):
            c["data"] = f.get("data") if f.get("data") in STRICTNESS else "cloud-ok"
            c.pop("concrete_models", None) if not f.get("concrete_models") else c.update(concrete_models=True)
        router.apply(raw, change=f"app {do.split('_')[1]}: {name}")
        if key:
            return f"API key for {name}: copy it now. RouterLM can't show it again.", key
        return f"App {name} saved.", None

    if do == "general":
        raw["listen"] = (f.get("listen") or "").strip()
        for k, label, low in (("cooldown_s", "Skip a failed model for", 1),
                              ("health_interval_s", "Check local AI apps every", 1)):
            v = number(f, k, label, float, low)
            raw.pop(k, None) if v is None else raw.update({k: v})
        raw["debug"] = bool(f.get("debug"))
        router.apply(raw, change="general settings changed")
        return "Saved." + (" Restart to listen on the new address." if router.restart_for else ""), None

    if do == "password":
        problem = new_password_problem(f.get("password", ""), f.get("repeat", ""))
        if problem:
            raise ValueError(problem)
        raw["admin_password"] = password_record(f["password"])
        router.apply(raw, change="admin password changed")
        return "Password changed. Your browser will ask for the new one.", None

    if do == "raw":
        try:
            new = json.loads(f.get("raw") or "")
        except ValueError as e:
            raise ValueError(f"that isn't valid JSON: {e}")
        if not isinstance(new, dict):
            raise ValueError("the config must be one JSON object")
        if "admin_password" not in new and cfg.admin_password:
            new["admin_password"] = cfg.admin_password  # deleting it here would open the page to anyone
        if f.get("imported"):  # the Import button: a file the person picked, saved through the same checks
            router.apply(new, change="settings imported")
            return ("Settings imported. A model whose API key isn't on this machine says \"API key missing\" until "
                    "you add the key under API keys."), None
        router.apply(new, change="config edited as text")
        return "Settings file saved.", None

    if do == "key_use" and "answer" in roles:
        value = read_secrets(cfg.base_dir).get(name) or ""
        provider, model = key_provider(value), (f.get("model") or "").strip()
        if not provider or not model:
            raise ValueError("pick a model for this API key")
        tid = add_target(targets, by_id, cloud_target(provider, model, name), name)
        rest = [t for t in roles["answer"].get("models") or [] if t != tid]
        roles["answer"]["models"] = [tid] + rest
        router.apply(raw, change=f"{tid} is #1 in answer")
        return f"{model_label(by_id[tid])} is now #1, the Primary, in the answer lineup" + (
            f"; then {', '.join(model_label(by_id[r]) if r in by_id else r for r in rest)}." if rest else "."), None

    if do in ("quick_start", "key_use"):
        if "answer" in roles:
            raise ValueError("there's already an answer lineup; change it below")
        if do == "key_use":  # no answer lineup yet: the first local model this machine runs goes behind it, if any
            picks = local_picks(find_engines(cfg))
            url, model = picks[0] if picks else ("", "")
            cloud = f"{name}|{(f.get('model') or '').strip()}"
        else:
            url, _, model = (f.get("pick") or "").partition("|")
            cloud = f.get("cloud") or ""
            if not url or not model:
                raise ValueError("pick a model")
        order = []
        if cloud:
            kname, _, cmodel = cloud.partition("|")
            provider = key_provider(read_secrets(cfg.base_dir).get(kname) or "")
            if not provider or not cmodel:
                raise ValueError("pick a cloud model, or None")
            order.append(add_target(targets, by_id, cloud_target(provider, cmodel, kname), kname))
        if url and model:
            order.append(add_target(targets, by_id, local_target(url, model), "local"))
        # A desktop's local model can be slow to start; with a cloud model first, a hang moves on sooner.
        roles["answer"] = {"models": order, "timeout_first_token_s": 60 if cloud else 120}
        router.apply(raw, change=f"quick start: answer = {', '.join(order)}")
        lineup = "; ".join(f"#{i}{' Primary' if i == 1 else ''} {model_label(by_id[tid])}"
                           for i, tid in enumerate(order, 1))
        return (f"Done. In your app, put http://{cfg.host}:{cfg.port}/v1 as the address and answer as the model. "
                f"On this Mac the app needs no API key: put its name in the API key box, and that's how it shows "
                f"on the main page. The answer lineup: {lineup}."), None

    if do == "engine_start":
        cmd = engine_starter(name)
        if not cmd:
            raise ValueError(f"{name} isn't installed where RouterLM looks for it")
        label = PRESETS[name][0].replace(" on this machine", "")
        if name == "ollama":  # runs in the foreground, so it's started on its own
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            time.sleep(2)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise ValueError(f"{label} didn't start: {(r.stderr or r.stdout).strip()[-300:]}")
        return f"{label} started. Its models are in the lists now.", None

    if do == "restart":
        return "Restarting.", None
    raise ValueError("unknown action")


def find_engines(cfg):
    """Local engines that answer, and the models each lists: this box's usual ports plus every local target's
    address. For the settings page's model lists only; nothing is sent to a cloud provider."""
    urls = [PRESETS[k][3] for k in ("lmstudio", "ollama", "llama-server")]
    found = {}
    for t in cfg.targets.values():
        if t.location == "local" and t.adapter == "openai" and t.base_url not in found:
            try:
                found[t.base_url] = get_models(t, 1)
            except Exception:  # not answering; the light already says so
                pass
    for url in urls:
        if url in found or url in (t.base_url for t in cfg.targets.values()):
            continue
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=1)
        try:
            conn.request("GET", u.path + "/models")
            resp = conn.getresponse()
            if resp.status == 200:
                found[url] = [m.get("id") for m in json.loads(resp.read()).get("data") or []]
        except Exception:  # nothing on that port
            pass
        finally:
            conn.close()
    return found


SETTINGS_CSS = """
a{color:var(--accent)}.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.gear{display:inline-flex;align-items:center;gap:6px;text-decoration:none}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px 12px;align-items:end}
label{display:block;font-size:12px;color:var(--muted)}label.check{display:flex;gap:6px;align-items:center;
color:var(--fg);font-size:14px}input,select,textarea{font:inherit;width:100%;box-sizing:border-box;padding:5px 7px;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}input[type=checkbox]{width:auto}
textarea{font:12px/1.4 ui-monospace,Menlo,monospace;min-height:320px}.row{display:flex;gap:8px;flex-wrap:wrap;
align-items:center;margin-top:8px}.ok{border:1px solid var(--green);color:var(--green);border-radius:8px;
padding:8px 12px;margin-top:12px}.key{font:13px ui-monospace,Menlo,monospace;user-select:all;word-break:break-all;
background:var(--bg);border:1px dashed var(--line);padding:8px;border-radius:6px;margin-top:6px}
button.danger{color:var(--red)}form.inline{display:inline}h3{font-size:14px;margin:0 0 6px}
a.button,label.button{display:inline-block;font-size:14px;padding:3px 10px;border-radius:6px;
border:1px solid var(--line);background:var(--bg);color:var(--accent);text-decoration:none;cursor:pointer}
"""


def page(title, body, refresh=None):
    meta = f"<meta http-equiv=refresh content='{refresh}'>" if refresh else ""
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>{meta}<title>{esc(title)}</title>"
            f"<style>{ADMIN_CSS}{SETTINGS_CSS}</style></head><body><main>{body}</main></body></html>")


def field(label, name, value="", kind="text", extra=""):
    return (f"<label>{esc(label)}<input type={kind} name={name} value='{esc(value)}' {extra}></label>")


def choose(label, name, options, current):
    opts = "".join(f"<option value='{esc(v)}'{' selected' if v == current else ''}>{esc(text)}</option>"
                   for v, text in options)
    return f"<label>{esc(label)}<select name={name}>{opts}</select></label>"


def form(do, inner, name=None, button="Save", cls=""):
    hidden = f"<input type=hidden name=do value={do}>" + (
        f"<input type=hidden name=name value='{esc(name)}'>" if name is not None else "")
    return (f"<form method=post action=/settings class='{cls}'>{hidden}{inner}"
            f"<div class=row><button>{esc(button)}</button></div></form>")


def delete_button(do, name, what):
    return (f"<form method=post action=/settings class=inline><input type=hidden name=do value={do}>"
            f"<input type=hidden name=name value='{esc(name)}'><button class=danger>Delete {esc(what)}</button></form>")


def restarting_page(router):
    host, port = router.cfg.host, router.cfg.port
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/settings"
    return page("Restarting", f"<h1>RouterLM <small>restarting</small></h1><section><p>Back in a few seconds at "
                              f"<a href='{esc(url)}'>{esc(url)}</a>.</p></section>", refresh=f"4;url={url}")


def key_options(keys, current):
    opts = [("", "No API key")] + [(f"secret:{k}", f"{k} (…{v[-4:]})") for k, v in keys.items()]
    if current and not current.startswith("secret:"):
        opts.append((current, current))  # a keychain:, file: or env: reference set in the file by hand
    return opts


def settings_page(router, note=None, error=None, posted=None, shown_key=None):
    cfg = router.cfg
    try:
        keys = read_secrets(cfg.base_dir)
    except ConfigError as e:
        keys, error = {}, f"{error}\n{e}" if error else str(e)
    raw = cfg.raw
    targets = [t for t in raw.get("models") or [] if isinstance(t, dict)]
    ids = [t.get("id") for t in targets]
    engines = find_engines(cfg)
    loaded = lmstudio_loaded(PRESETS["lmstudio"][3]) if PRESETS["lmstudio"][3] in engines else set()
    out = [f"<div class=top><h1>RouterLM <small>settings</small></h1><a href=/admin>← Main page</a></div>"]
    if note:
        out.append(f"<div class=ok>{esc(note)}</div>")
    if shown_key:
        out.append(f"<div class=key>{esc(shown_key)}</div>")
    if error:
        out.append(f"<div class=banner><b>Not saved.</b><br>{esc(error).replace(chr(10), '<br>')}</div>")
    if not raw.get("lineups") and not shown_key:
        picks = [(f"{u}|{m}", local_label(u, m, loaded)) for u, m in local_picks(engines)]
        clouds = [("", "None: this machine only")] + [
            (f"{k}|{m}", f"{m} ({PRESETS[key_provider(v)][0]}, API key {k})")
            for k, v in keys.items() for m in provider_models(router, v)[0]]
        if picks:
            out.append("<section><h2>Quick start</h2><p>Pick a model this machine already runs, and a cloud model to "
                       "be the Primary (#1) if you have an API key. RouterLM sets up an <code>answer</code> lineup "
                       "for your apps, in one step.</p>"
                       + form("quick_start", "<div class=grid>" + choose("Model on this machine", "pick", picks,
                                                                         picks[0][0])
                              + (choose("Cloud model as Primary", "cloud", clouds, "") if len(clouds) > 1 else "")
                              + "</div>", button="Use it") + "</section>")
        else:
            out.append("<section><h2>Quick start</h2><p>No local AI app answered on this machine. Start LM Studio's "
                       "server, Ollama or llama-server and reload this page, or add a cloud model below.</p></section>")
    if router.restart_for:
        out.append("<div class=banner>A saved change needs a restart: "
                   f"{esc(router.restart_for)}. " + form("restart", "", button="Restart now", cls="inline") + "</div>")

    # API keys
    def used_by(k, v):
        users = [model_label(t) for t in targets if t.get("api_key") == f"secret:{k}"]
        if users:
            return "<br>".join(esc(u) for u in users)
        models, problem = provider_models(router, v)
        if problem:
            return f"<span class=bad>{esc(problem)}</span>"
        if not models:
            return "<span class=muted>add a model below and choose this API key</span>"
        return form("key_use", choose("", "model", [(m, m) for m in models], models[0]), k, "Use it", "inline")

    rows = [[f"<b>{esc(k)}</b>", f"…{esc(v[-4:])}", used_by(k, v),
             form("key_replace", "<input type=password name=value placeholder='New API key' autocomplete=off>", k,
                  "Replace", "inline") + " " + delete_button("key_delete", k, "")] for k, v in keys.items()]
    out.append("<section><h2>API keys</h2><p class=muted>API keys from Anthropic, OpenAI and other cloud model "
               "companies. Several from one company are fine: two accounts can back each other up. To change which "
               "models use a key, pick the key on the model's card under Models. API keys stay on this machine, "
               "in a file only RouterLM's user account can open, and after you save one this page shows only its "
               "last 4 characters.</p>" + table(["Name", "Ends with", "Used by", ""], rows, "No API keys yet.")
               + form("key_add", "<div class=grid>" + field("Name", "name", free_name("anthropic", keys))
                      + field("API key", "value", kind="password", extra="autocomplete=off") + "</div>",
                      button="Add API key")
               + "</section>")

    # Models
    usable = []  # (value, label) for models a role can take that aren't set up yet
    for k, v in keys.items():
        provider = key_provider(v)
        for m in provider_models(router, v)[0]:
            if not any(t.get("model") == m and t.get("api_key") == f"secret:{k}" for t in targets):
                usable.append((f"new:{k}|{m}", f"{m} ({PRESETS[provider][0]}, API key {k})"))
    for u, m in local_picks(engines):
        if not any(t.get("model") == m and t.get("base_url") == u for t in targets):
            usable.append((f"new-local:{u}|{m}", local_label(u, m, loaded)))
    names = {m for ms in engines.values() for m in ms} | {label.split(" (")[0] for _, label in usable}
    datalist = "<datalist id=models>" + "".join(f"<option value='{esc(m)}'>" for m in sorted(names)) + "</datalist>"
    rows = []
    for preset in ENGINES:
        label, url = PRESETS[preset][0].replace(" on this machine", ""), PRESETS[preset][3]
        if url in engines:
            state = f"running, {plural(len(engines[url]), 'model')}" + (
                f", {len(loaded)} loaded" if preset == "lmstudio" and loaded else "")
            rows.append([f"<b>{esc(label)}</b>", esc(state), ""])
        elif engine_starter(preset):
            rows.append([f"<b>{esc(label)}</b>", "installed, not running",
                         form("engine_start", "", preset, f"Start {label}", "inline")])
        else:
            rows.append([f"<b>{esc(label)}</b>", "<span class=muted>not found on this machine</span>", ""])
    rows += [[esc(u), f"running, {plural(len(ms), 'model')}", ""] for u, ms in engines.items()
             if u not in (PRESETS[p][3] for p in ENGINES)]
    found = table(["Local AI app", "On this machine", ""], rows, "")
    cards = []
    for t in targets:
        tid = t.get("id")
        inner = ("<div class=grid>"
                 + field("Model name, as the company or local AI app lists it", "model", t.get("model"),
                         extra="list=models")
                 + field("Web address", "base_url", t.get("base_url"))
                 + choose("Where it runs", "location", [("local", "In the building (local)"),
                                                        ("cloud", "In the cloud")], t.get("location"))
                 + choose("Type", "adapter", [("openai", "OpenAI-style: OpenAI, LM Studio, Ollama, llama-server and "
                                                         "most others"), ("anthropic", "Anthropic")], t.get("adapter"))
                 + choose("API key", "api_key", key_options(keys, t.get("api_key")), t.get("api_key") or "")
                 + field("Questions it works on at once (blank: 2 local, 8 cloud)", "max_parallel",
                         t.get("max_parallel", ""), "number", "min=1")
                 + field("File fingerprint (optional, local models)", "weights_sha256", t.get("weights_sha256", ""))
                 + field("Model file location (only with a fingerprint, if the local AI app doesn't say)",
                         "weights_path", t.get("weights_path", ""))
                 + "</div>")
        state = cfg.targets[tid].state() if tid in cfg.targets else {"state": "?"}
        mark = "" if state["state"] == "up" else f" <span class=bad>{esc(state['state'])}</span>"
        cards.append(f"<div class=card><h3>{esc(model_label(t))}{mark}</h3>{form('target_save', inner, tid)}"
                     f"<div class=row>{delete_button('target_delete', tid, 'model')}</div></div>")
    add = ("<div class=grid>" + choose("Where it runs", "preset", [(k, v[0]) for k, v in PRESETS.items()], "lmstudio")
           + field("Model name", "model", extra="list=models placeholder='as the local AI app or company lists it'")
           + choose("API key (cloud)", "key", key_options(keys, None), "")
           + field("Web address (only for the two 'give its address' choices)", "base_url") + "</div>")
    out.append("<section><h2>Models</h2><p class=muted>The models you can put in a lineup: in the cloud, or run "
               "by a local AI app in the building. A local model can be locked to one exact file with its file "
               "fingerprint; if the file changes, the next model in the lineup answers until the new file has "
               "passed the test questions.</p>"
               f"{found}{datalist}{''.join(cards)}"
               f"<div class=card><h3>Add a model</h3>{form('target_add', add, button='Add model')}</div></section>")

    # Lineups
    choices = [("", "—")] + [(t.get("id"), model_label(t, str(t.get("api_key") or "")[7:] or None)
                              if str(t.get("api_key") or "").startswith("secret:") else model_label(t))
                             for t in targets] + usable
    cards = []
    for rname, r in (raw.get("lineups") or {}).items():
        order = list(r.get("models") or [])
        slots = "".join(choose(f"#{i + 1}{' Primary' if i == 0 else ''}", f"t{i}", choices,
                               order[i] if i < len(order) else "") for i in range(len(order) + 1))
        inner = ("<div class=grid>" + slots
                 + choose("Priority", "lane", [("interactive", "A person is waiting: goes first"),
                                               ("batch", "Background work: waits until no one is waiting")],
                          r.get("lane", "interactive"))
                 + choose("Which models", "data", WHERE, r.get("data", "cloud-ok"))
                 + field("Move on if no answer starts within (seconds)", "timeout_first_token_s",
                         r.get("timeout_first_token_s", ""), "number", "step=any min=0.1")
                 + field("Longest question, in tokens of about 4 characters (blank: no limit)", "max_context",
                         r.get("max_context", ""), "number", "min=1")
                 + "</div>")
        light = router.light(cfg.roles[rname]) if rname in cfg.roles else "grey"
        cards.append(f"<div class=card><h3><span class='dot {light}'></span>{esc(rname)}</h3>"
                     f"{form('role_save', inner, rname)}<div class=row>{delete_button('role_delete', rname, 'lineup')}"
                     "</div></div>")
    add = "<div class=grid>" + field("Name", "name", free_name("answer", raw.get("lineups") or {})) + choose(
        "#1 Primary", "t0", choices, "") + "</div>"
    out.append("<section><h2>Lineups</h2><p class=muted>An app asks for a lineup by name, such as "
               "<code>answer</code>. #1 is the Primary and answers while it can. When it can't, #2 answers, and so on "
               "down. You decide the order and where it ends. Every model your API keys and this machine's local AI "
               "apps offer is in the lists; pick one and Save. A lineup for search indexing (embeddings) is named "
               f"<code>embed</code> and has one local model.</p>{''.join(cards)}<div class=card><h3>Add a lineup</h3>"
               f"{form('role_add', add, button='Add lineup')}</div></section>")

    # Apps
    rows = []
    for cname, c in (raw.get("apps") or {}).items():
        inner = (choose("Which models", "data", WHERE, c.get("data", "cloud-ok"))
                 + "<label class=check><input type=checkbox name=concrete_models value=1"
                 + (" checked" if c.get("concrete_models") else "") + "> May ask for a model by name, not only a "
                 "lineup</label>")
        rows.append([f"<b>{esc(cname)}</b>", form("caller_save", inner, cname, "Save", "inline"),
                     form("caller_key", "", cname, "New API key", "inline") + " "
                     + delete_button("caller_delete", cname, "")])
    add = ("<div class=grid>" + field("Name", "name", free_name("my-app", raw.get("apps") or {}))
           + choose("Which models", "data", WHERE, "cloud-ok")
           + "<label class=check><input type=checkbox name=concrete_models value=1> May ask for a model by name"
           "</label></div>")
    out.append("<section><h2>Apps</h2><p class=muted>An app sends questions to RouterLM: Capsule, a chat window. "
               f"In the app's settings, put <code>http://{esc(cfg.host)}:{cfg.port}/v1</code> "
               "as the address and a lineup's name as the model.</p><p class=muted>An app on this Mac needs no API "
               "key. Put its name in the app's API key box, and that's how it shows on the main page; left blank, "
               "it shows as \"this Mac\". To give it settings of its own, add it below under that name. An app on "
               "another computer needs an API key: add it below and copy the key into the app.</p>"
               + table(["App", "Settings", ""], rows, "None added. Apps on this Mac work without being added.")
               + form("caller_add", add, button="Add app and make its API key") + "</section>")

    # General, password, the file
    inner = ("<div class=grid>" + field("RouterLM's address on the network (a change needs a restart)", "listen",
                                        raw.get("listen", ""))
             + field("After a model fails, skip it for (seconds)", "cooldown_s", raw.get("cooldown_s", ""), "number",
                     "step=any min=1")
             + field("Check local AI apps every (seconds)", "health_interval_s", raw.get("health_interval_s", ""),
                     "number", "step=any min=1")
             + "<label class=check><input type=checkbox name=debug value=1" + (" checked" if raw.get("debug") else "")
             + "> Troubleshooting: also save every question and answer in full, in a separate file</label></div>")
    out.append("<section><h2>General</h2>" + form("general", inner)
               + form("restart", "", button="Restart RouterLM", cls="inline") + "</section>")
    inner = ("<div class=grid>" + field("New password", "password", kind="password", extra="autocomplete=new-password")
             + field("Same again", "repeat", kind="password", extra="autocomplete=new-password") + "</div>")
    state = "A password is set." if cfg.admin_password else "Off: no password is set, so only this machine can open it."
    out.append("<section><h2>Opening this page from other computers</h2><p class=muted>On this machine no password "
               f"is asked. {state} Setting one lets other computers on the office network open the main page and "
               "Settings with it.</p>" + form("password", inner, button="Set password") + "</section>")
    text = posted["raw"] if posted and posted.get("do") == "raw" else json.dumps(raw, indent=2, ensure_ascii=False)
    out.append("<section><h2>The settings file</h2><p class=muted>Everything above as text, for whoever would rather "
               f"edit the file itself (<code>{esc(cfg.path)}</code>). API keys aren't in it. Saving it goes through "
               "the same checks as the forms above.</p>"
               "<div class=row><a class=button href=/settings/export download=routerlm.json>Export</a>"
               "<label class=button>Import<input type=file id=import accept='.json,application/json' hidden>"
               "</label></div><p class=muted>Export downloads this file, to keep or to set up another RouterLM the "
               "same way. Import loads a file you pick and saves it through the same checks. API keys never go with "
               "it: after an import, a model whose API key isn't on this machine says \"API key missing\" until you "
               "add the key under API keys.</p>"
               + form("raw", f"<textarea name=raw spellcheck=false>{esc(text)}</textarea>"
                      "<input type=hidden name=imported value=''>", button="Save file")
               + "</section><script>document.getElementById('import').onchange=function(){var f=this.files[0];"
               "if(!f)return;f.text().then(function(t){var m=document.querySelector('textarea[name=raw]').form;"
               "m.raw.value=t;m.imported.value='1';m.submit()})}</script>")
    return page("RouterLM settings", "".join(out))


# ---------------------------------------------------------------------------------------------------------
# The front door (sections 3 and 12)

class Handler(BaseHTTPRequestHandler):
    server_version = f"RouterLM/{VERSION}"
    max_body = 64 * 1024 * 1024

    def log_message(self, *args):
        pass  # RouterLM keeps its own log

    @property
    def router(self):
        return self.server.router

    def key_hash(self):
        auth = self.headers.get("Authorization") or ""
        return sha256(auth[7:].strip()) if auth[:7].lower() == "bearer " else None

    def caller(self):
        """The app asking. An app on the RouterLM machine needs no API key (Rene 10/15): whatever is in its API key
        box is its name on the page, and the settings of an app by that name in Settings apply; with nothing there,
        or something that looks like a key, it shows as "this Mac". Other computers need an app's API key."""
        cfg = self.router.cfg
        c = cfg.callers.get(self.key_hash())
        if c is not None:
            return c
        if not self.from_this_machine():
            raise Refuse(401, "missing or unknown API key", "invalid_api_key")
        if not self.addressed_here():
            raise Refuse(403, "use RouterLM at its own address, e.g. http://127.0.0.1:<port>/v1", "bad_host")
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            raise Refuse(403, "a web page from another site needs an app's API key", "bad_origin")
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        name = clean_name(token)
        if not name or len(name) > 32 or token.startswith(("sk-", "rlm_")):
            name = THIS_MAC  # never show a key on the page or in the log
        return cfg.app_names.get(name) or Caller(name, {})

    def given_password(self):
        """The admin password as a bearer token, or as the password of a browser's own login (HTTP Basic)."""
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        if auth[:6].lower() == "basic ":
            try:
                return base64.b64decode(auth[6:].strip()).decode("utf-8").partition(":")[2]
            except ValueError:  # bad base64 or not UTF-8
                return None
        return None

    def from_this_machine(self):
        """A request from the RouterLM machine itself: a loopback address, or the machine's own listen address."""
        ip = self.client_address[0]
        try:
            return ipaddress.ip_address(ip.split("%")[0]).is_loopback or ip == self.server.server_address[0]
        except ValueError:
            return False

    def addressed_here(self):
        """The Host the browser asked for is this machine by a name it has. A web page that points its own name at
        127.0.0.1 (DNS rebinding) sends its own name instead, and is refused."""
        host = (urllib.parse.urlsplit("//" + (self.headers.get("Host") or "")).hostname or "").lower()
        name = socket.gethostname().lower()
        return host in ("127.0.0.1", "localhost", "::1", self.router.cfg.host, self.server.server_address[0],
                        name, name + ".local") or host.endswith(".localhost")

    def admin(self):
        """The admin and settings pages. On the RouterLM machine itself: no password, since nothing here is more
        private than that machine's own files. Other computers: the admin password, once one is set in Settings."""
        if self.from_this_machine():
            if not self.addressed_here():
                raise Refuse(403, "open RouterLM at its own address, e.g. http://127.0.0.1:<port>/", "bad_host")
            return
        if not self.router.cfg.admin_password:
            raise Refuse(403, "other computers can open this page once a password is set in Settings on the RouterLM "
                              "machine itself", "admin_off")
        if not self.router.password_ok(self.given_password()):
            raise Refuse(401, "the admin page needs the admin password", "invalid_admin_password",
                         headers={"WWW-Authenticate": 'Basic realm="RouterLM admin", charset="UTF-8"'})

    def same_origin(self):
        """Forms only from RouterLM's own pages: a browser sends a saved login with any site's form."""
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            raise Refuse(403, "the admin page takes forms only from itself", "bad_origin")

    def form(self, limit=1 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise Refuse(413, "the form is too large", "too_large")
        body = self.rfile.read(n).decode("utf-8", "replace") if n > 0 else ""
        return {k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}

    def send_html(self, status, text, headers=None):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def see_other(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status, obj, target=None, headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_raw(status, raw, target, headers)

    def send_raw(self, status, raw, target=None, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.labels(target)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def labels(self, t):
        if t is not None:
            self.send_header("X-RouterLM-Model", t.id)
            self.send_header("X-RouterLM-Location", t.location)

    def refuse(self, r):
        try:
            if r.body:
                self.send_raw(r.status, r.body, r.target, r.headers)
            else:
                self.send_json(r.status, {"error": {"message": r.message, "type": r.code, "code": r.code}},
                               r.target, r.headers)
        except OSError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/v1/models":
                c = self.caller()
                data = [{"id": n, "object": "model", "created": 0, "owned_by": "routerlm"} for n in self.router.cfg.roles]
                if c.concrete:
                    data += [{"id": m, "object": "model", "created": 0, "owned_by": t.id}
                             for m, t in self.router.cfg.by_model.items() if m not in self.router.cfg.roles]
                self.send_json(200, {"object": "list", "data": data})
            elif path == "/status":
                local = self.from_this_machine() and self.addressed_here()
                if not local and not self.router.password_ok(self.given_password()):
                    self.caller()
                self.send_json(200, self.router.status())
            elif path in ("/", "/admin"):
                self.admin()
                if path == "/" and not self.router.cfg.roles:
                    return self.see_other("/settings")  # nothing to show yet; /admin always shows the page
                self.send_html(200, admin_page(self.router))
            elif path == "/settings":
                self.admin()
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.send_html(200, settings_page(self.router, note=(query.get("saved") or [None])[0]))
            elif path == "/settings/export":  # the settings file as it is on disk; the API keys live elsewhere
                self.admin()
                cfg = self.router.cfg
                try:
                    with open(cfg.path, "rb") as f:
                        raw = f.read()
                except (OSError, TypeError):  # started without a file
                    raw = (json.dumps(cfg.raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", 'attachment; filename="routerlm.json"')
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            else:
                raise Refuse(404, "no such endpoint", "not_found")
        except Refuse as r:
            self.refuse(r)
        except Exception as e:
            self.crashed(e)

    def crashed(self, e):
        import traceback
        traceback.print_exc()
        self.refuse(Refuse(500, f"RouterLM error: {e.__class__.__name__}: {e}", "internal_error"))

    def check_set(self):
        """The admin page's Run test questions button."""
        self.admin()
        self.same_origin()
        self.router.start_check(self.form(4096).get("model", ""))
        self.see_other("/")

    def settings(self):
        self.admin()
        self.same_origin()
        f = self.form()
        try:
            note, shown = settings_action(self.router, f)
        except (ConfigError, ValueError) as e:
            return self.send_html(400, settings_page(self.router, error=str(e), posted=f))
        if shown:  # a new app key: shown once, never put in a URL
            return self.send_html(200, settings_page(self.router, note=note, shown_key=shown))
        if f.get("do") == "restart":
            self.send_html(200, restarting_page(self.router))
            self.wfile.flush()
            threading.Timer(0.5, restart).start()
            return None
        self.see_other("/settings?" + urllib.parse.urlencode({"saved": note}))

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/test-questions":
                return self.check_set()
            if path == "/settings":
                return self.settings()
            if path == "/try":
                self.admin()
                self.same_origin()
                f = self.form(1 << 20)
                return self.send_html(200, admin_page(self.router, try_it(self.router, f), f))
            caller = self.caller()
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > self.max_body:
                raise Refuse(400, "send a JSON body with a Content-Length", "bad_request")
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                raise Refuse(400, "the body isn't valid JSON", "bad_request")
            if not isinstance(body, dict):
                raise Refuse(400, "the body must be a JSON object", "bad_request")
            if path == "/v1/chat/completions":
                self.chat(caller, body)
            elif path == "/v1/embeddings":
                self.embeddings(caller, body)
            else:
                raise Refuse(404, "no such endpoint", "not_found")
        except Refuse as r:
            self.refuse(r)
        except Exception as e:
            self.crashed(e)

    def chat(self, caller, body):
        role, data, lane = self.router.resolve(caller, body, self.headers, embeddings=False)
        call = Call(caller.name, role.name, data, lane, body)
        try:
            ans = self.router.open_chat(call, role)
        except Refuse as r:
            call.outcome, call.target = r.code, r.target
            self.router.finish(call)
            raise
        try:
            if body.get("stream"):
                self.stream_out(call, ans, body)
            else:
                self.json_out(call, ans)
        finally:
            ans.close()
            self.router.finish(call)

    def stream_out(self, call, ans, body):
        t = ans.target
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.labels(t)
            self.end_headers()
            try:
                for ch in ans.chunks():
                    call.usage = ch.get("usage") or call.usage
                    if not ch.get("choices") and ch.get("usage") and not want_usage:
                        continue
                    ch["model"] = t.model
                    if self.router.cfg.debug:
                        call.text += [(c.get("delta") or {}).get("content") or "" for c in ch.get("choices") or []]
                    self.wfile.write(b"data: " + json.dumps(ch, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
            except TargetError as e:
                # Section 10: once tokens flow, a broken stream ends with an error. Never a second model.
                call.outcome = "stream_broken"
                err = {"error": {"message": f"{e.message}; the answer broke off, retry the request",
                                 "type": "stream_broken", "code": "stream_broken"}}
                self.wfile.write(b"data: " + json.dumps(err).encode("utf-8") + b"\n\n")
                return
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            call.outcome = "app_gone"

    def json_out(self, call, ans):
        t = ans.target
        try:
            out = assemble(ans.chunks(), t)
        except TargetError as e:
            call.outcome = "stream_broken"
            raise Refuse(502, f"{e.message}; the answer broke off, retry the request", "stream_broken", target=t)
        call.usage = out.get("usage")
        if self.router.cfg.debug:
            call.text = [(c["message"].get("content") or "") for c in out["choices"]]
        try:
            self.send_json(200, out, t)
        except (BrokenPipeError, ConnectionResetError):
            call.outcome = "app_gone"

    def embeddings(self, caller, body):
        role, data, lane = self.router.resolve(caller, body, self.headers, embeddings=True)
        call = Call(caller.name, role.name, data, lane, body)
        try:
            t, obj = self.router.embed(call, role)
        except Refuse as r:
            call.outcome, call.target = r.code, r.target
            self.router.finish(call)
            raise
        self.router.finish(call)
        self.send_json(200, obj, t)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, router):
        self.router = router
        if ":" in router.cfg.host:
            self.address_family = socket.AF_INET6
        super().__init__((router.cfg.host, router.cfg.port), Handler)

    def stop(self):
        self.router.stop_event.set()
        self.shutdown()
        self.server_close()


def start(cfg):
    """Start RouterLM in background threads and return the server; tests and embedders use this."""
    srv = Server(Router(cfg))
    threading.Thread(target=srv.router.health_loop, daemon=True).start()
    threading.Thread(target=srv.serve_forever, args=(0.1,), daemon=True).start()
    return srv


STARTER = {"_note": "Made on first start. Set RouterLM up in a browser at the listen address; Settings writes here.",
           "listen": "127.0.0.1:8090"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="routerlm", description="One endpoint in front of every model an office uses.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    subs = {}
    for name, text in (("serve", "run the service"), ("check", "check the config and exit"),
                       ("test-questions", "ask the test questions of one model (section 13)"),
                       ("grade", "review a test-questions run's grades, one answer at a time")):
        subs[name] = sub.add_parser(name, help=text)
        subs[name].add_argument("--config", default="routerlm.json")
    subs["test-questions"].add_argument("--model", required=True, help="the model's id, as Settings shows it")
    subs["grade"].add_argument("--run", help="a run file; the newest run when left out")
    sub.add_parser("new-key", help="make an app's API key and the key_sha256 that goes under apps in the config")
    args = ap.parse_args(argv)
    if args.cmd == "new-key":
        key = "rlm_" + secrets.token_urlsafe(32)
        print(f"API key (give to the app):  {key}\nkey_sha256 (put in config): {sha256(key)}")
        return 0
    if args.cmd == "serve" and not os.path.exists(args.config):
        write_private(args.config, json.dumps(STARTER, indent=2) + "\n")
        print(f"new config at {os.path.abspath(args.config)}; finish setting up in a browser", flush=True)
    try:
        cfg = read_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if args.cmd == "check":
        print(f"config ok: {len(cfg.roles)} lineups, {len(cfg.targets)} models, {len(cfg.callers)} apps")
        for t in cfg.targets.values():
            if t.key_problem:
                print(f"  out of use until its API key is added: {t.key_problem}")
        return 0
    if args.cmd == "test-questions":
        try:
            cs = load_check_set(cfg)
        except ConfigError as e:
            print(e, file=sys.stderr)
            return 2
        why = check_refusal(cfg, cs, args.model)
        if why:
            print(why, file=sys.stderr)
            return 2
        print(f"asking {len(cs['questions'])} questions of {args.model}, one at a time ...", flush=True)
        path, run = run_check_set(cfg, cs, cfg.targets[args.model], Log(cfg.log_dir, False))
        print(run_summary(cfg, run) + f"\n  run file: {path}")
        return 0
    if args.cmd == "grade":
        path = args.run or next(iter(run_files(cfg)), None)
        if not path:
            print(f"no runs in {os.path.join(cfg.check_dir, 'runs')}", file=sys.stderr)
            return 2
        grade_cli(cfg, path)
        return 0
    try:
        srv = Server(Router(cfg))
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"Port {cfg.port} on {cfg.host} is already taken. If RouterLM is already running, open "
              f"http://{cfg.host}:{cfg.port}/ instead. Otherwise change listen in {cfg.path}.", file=sys.stderr)
        return 1
    threading.Thread(target=srv.router.health_loop, daemon=True).start()
    print(f"RouterLM {VERSION} listening on {cfg.host}:{cfg.port}. Admin page and settings: "
          f"http://{cfg.host}:{cfg.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
