# test_spec.py v0.18 — 2026-10-15
# Changelog (latest two; older entries: `git log -- RouterLM/impl/tests/test_spec.py`):
# - v0.18 (2026-10-15): Export and Import in Settings (Rene: "exports leave the API keys out").
# - v0.17 (2026-10-15): No short name on the screens (Rene: one way, the model's name and where it runs): Add a
#   model makes it, the same model added twice is refused in plain words, a second API key gets its own entry.




"""Run from the folder with routerlm.py in it:  python3 -m unittest discover -s tests -v"""
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import routerlm  # noqa: E402
from stubs import BlackHole, Stub, closed_port  # noqa: E402

KEYS = {"capsule": "rlm_capsule", "hr-reports": "rlm_hr", "owner-desk": "rlm_owner", "admin": "rlm_admin"}
PROMPT = "What did Q-1042 quote for the stage lights? PRIVATE-MARKER-7731"
T = 1.0  # first-token timeout in these tests, seconds


def h(key):
    return hashlib.sha256(key.encode()).hexdigest()


def sketch(claude, openai, local, embedder, log_dir):
    """The section 15 sketch, pointed at stubs."""
    return {
        "listen": "127.0.0.1:0", "cooldown_s": 60, "health_interval_s": 0.1, "log_dir": log_dir,
        "admin_password": routerlm.password_record(KEYS["admin"], 1000),
        "apps": {
            "capsule": {"key_sha256": h(KEYS["capsule"]), "data": "cloud-ok"},
            "hr-reports": {"key_sha256": h(KEYS["hr-reports"]), "data": "local-only"},
            "owner-desk": {"key_sha256": h(KEYS["owner-desk"]), "data": "cloud-ok", "concrete_models": True},
        },
        "models": [
            {"id": "cloud-claude", "location": "cloud", "adapter": "anthropic", "base_url": claude,
             "model": "claude-opus-5-5", "api_key": "env:RLM_TEST_ANTHROPIC"},
            {"id": "cloud-openai", "location": "cloud", "adapter": "openai", "base_url": openai,
             "model": "gpt-5.5", "api_key": "env:RLM_TEST_OPENAI"},
            {"id": "local-qwen27", "location": "local", "adapter": "openai", "base_url": local,
             "model": "qwen3.6-27b-q4_k_m"},
            {"id": "local-embed", "location": "local", "adapter": "openai", "base_url": embedder,
             "model": "nomic-embed-text-v2"},
        ],
        "lineups": {
            "answer": {"lane": "interactive", "data": "cloud-ok", "timeout_first_token_s": T, "max_context": 32768,
                       "models": ["cloud-claude", "cloud-openai", "local-qwen27"]},
            "summarize": {"lane": "batch", "data": "cloud-ok", "timeout_first_token_s": T, "max_context": 32768,
                          "models": ["local-qwen27"]},
            "reason": {"lane": "interactive", "data": "cloud-ok", "timeout_first_token_s": T, "max_context": 65536,
                       "models": ["cloud-claude", "cloud-openai", "local-qwen27"]},
            "embed": {"lane": "batch", "data": "local-only", "models": ["local-embed"]},
        },
    }


class Resp:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw

    @property
    def target(self):
        return self.headers.get("X-RouterLM-Model")

    @property
    def location(self):
        return self.headers.get("X-RouterLM-Location")

    def json(self):
        return json.loads(self.raw)

    def events(self):
        return [line[6:] for line in self.raw.decode().split("\n") if line.startswith("data: ")]

    def text(self):
        if self.headers.get("Content-Type") == "text/event-stream":
            out = []
            for e in self.events():
                if e != "[DONE]":
                    out += [(c.get("delta") or {}).get("content") or "" for c in json.loads(e).get("choices") or []]
            return "".join(out)
        return self.json()["choices"][0]["message"]["content"]


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["RLM_TEST_ANTHROPIC"] = "sk-ant-test"
        os.environ["RLM_TEST_OPENAI"] = "sk-openai-test"
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = Stub("anthropic", "claude-opus-5-5", key="sk-ant-test")
        self.openai = Stub("openai", "gpt-5.5", key="sk-openai-test")
        self.local = Stub("openai", "qwen3.6-27b-q4_k_m")
        self.embedder = Stub("openai", "nomic-embed-text-v2")
        for s in (self.claude, self.openai, self.local, self.embedder):
            self.addCleanup(s.stop)
        self.raw = sketch(self.claude.url, self.openai.url, self.local.url, self.embedder.url, self.tmp)
        self.srv = None

    def target(self, tid):
        return next(t for t in self.raw["models"] if t["id"] == tid)

    def start(self):
        path = os.path.join(self.tmp, "routerlm.json")
        with open(path, "w") as f:
            json.dump(self.raw, f)
        self.srv = routerlm.start(routerlm.read_config(path))
        self.addCleanup(self.srv.stop)
        self.port = self.srv.server_address[1]
        time.sleep(0.15)  # one health pass

    def request(self, method, path, body=None, key="capsule", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Content-Type": "application/json"}
        if key:
            hdrs["Authorization"] = f"Bearer {KEYS[key]}"
        hdrs.update(headers or {})
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, hdrs)
        r = conn.getresponse()
        resp = Resp(r.status, {k: v for k, v in r.getheaders()}, r.read())
        conn.close()
        return resp

    def ask(self, role="answer", key="capsule", stream=False, data=None, **extra):
        body = {"model": role, "stream": stream, "temperature": 0.2,
                "messages": [{"role": "system", "content": "Answer from the records."},
                             {"role": "user", "content": PROMPT}]}
        body.update(extra)
        return self.request("POST", "/v1/chat/completions", body, key,
                            {"X-RouterLM-Data": data} if data else None)

    def status(self):
        return self.request("GET", "/status", key="admin").json()

    def basic(self, key=None):
        return "Basic " + base64.b64encode(f"owner:{key or KEYS['admin']}".encode()).decode()

    def raw_request(self, method, path, headers, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        resp = Resp(r.status, {k: v for k, v in r.getheaders()}, r.read())
        conn.close()
        return resp

    def page(self, auth=None):
        return self.raw_request("GET", "/", {"Authorization": auth} if auth else {})

    def as_other_computer(self):
        """Requests from here on look like they come from another machine on the office network."""
        real = routerlm.Handler.from_this_machine
        routerlm.Handler.from_this_machine = lambda handler: False
        self.addCleanup(setattr, routerlm.Handler, "from_this_machine", real)

    def run_button(self, target, auth=None, origin=None):
        return self.raw_request("POST", "/test-questions", {
            "Authorization": auth or self.basic(), "Origin": origin or f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/x-www-form-urlencoded"}, f"model={target}".encode())

    def light(self, role):
        return self.status()["lineups"][role]["light"]

    def dots(self, role):
        return [m["light"] for m in self.status()["lineups"][role]["models"]]

    def alerts(self):
        return {(a["model"], a["alert"]) for a in self.status()["alerts"]}

    def log_lines(self, at_least=1):
        """Request lines from the log. RouterLM writes a line after the answer is sent, so wait for it."""
        deadline = time.monotonic() + 2
        while True:
            lines = []
            for name in sorted(os.listdir(self.tmp)):
                if name.startswith("routerlm-"):
                    with open(os.path.join(self.tmp, name)) as f:
                        lines += [json.loads(x) for x in f if x.strip()]
            lines = [x for x in lines if "event" not in x]
            if len(lines) >= at_least or time.monotonic() > deadline:
                return lines
            time.sleep(0.02)


# ---------------------------------------------------------------------------------------------------------
# Section 17's table, row by row, with injected faults (section 18, first bullet)

class FailureTable(Base):
    def test_first_target_answers_green(self):
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target, r.location), (200, "cloud-claude", "cloud"))
        self.assertEqual(r.json()["model"], "claude-opus-5-5")
        self.assertEqual(r.text().strip(), "hello from claude-opus-5-5")
        self.assertEqual(self.light("answer"), "green")

    def test_engine_hangs_no_first_token(self):
        hole = BlackHole()
        self.addCleanup(hole.close)
        self.target("cloud-claude")["base_url"] = hole.url
        self.start()
        t0 = time.monotonic()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertGreaterEqual(time.monotonic() - t0, T * 0.9)
        self.assertEqual(self.light("answer"), "yellow")
        self.assertEqual(self.log_lines()[-1]["failover_from"], "cloud-claude")

    def test_headers_arrive_but_no_token(self):
        self.claude.mode = "slow_first"  # message_start and a ping arrive; no content
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertEqual(self.light("answer"), "yellow")

    def test_5xx(self):
        self.claude.mode = "500"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertEqual(self.light("answer"), "yellow")
        self.assertEqual(self.alerts(), set())

    def test_429_rate_limited(self):
        self.claude.mode = "429"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertEqual(self.alerts(), set())

    def test_key_rejected_alerts(self):
        self.claude.mode = "401"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertEqual(self.alerts(), {("cloud-claude", "API key rejected")})
        self.assertEqual(self.light("answer"), "yellow")

    def test_anthropic_out_of_credit_is_a_400_and_still_fails_over(self):
        self.claude.mode = "no_credit"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"))
        self.assertEqual(self.alerts(), {("cloud-claude", "account out of credit")})

    def test_openai_out_of_credit_is_a_429_and_alerts(self):
        self.claude.mode = "500"
        self.openai.mode = "no_credit"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target, r.location), (200, "local-qwen27", "local"))
        self.assertEqual(self.alerts(), {("cloud-openai", "account out of credit")})

    def test_local_engine_crashes(self):
        self.local.stop()
        self.start()
        t0 = time.monotonic()
        r = self.ask("summarize")
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_model"))
        self.assertLess(time.monotonic() - t0, 0.5, "red fails fast")
        self.assertEqual(self.light("summarize"), "red")
        self.assertEqual(self.light("answer"), "green", "a cloud-first role still has its first target")

    def test_engine_reports_a_different_model(self):
        self.local.listed = ["qwen3.6-27b-q8_0"]  # someone loaded a different file
        self.start()
        self.assertEqual(self.light("summarize"), "red")
        self.assertIn(("local-qwen27", "local AI app reports a different model"), self.alerts())
        r = self.ask("summarize")  # nothing else in the lineup: a question tests it ("test when testable", Rene)
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))
        self.local.listed = None
        time.sleep(0.3)
        self.assertEqual(self.ask("summarize").status, 200)
        self.assertEqual(self.alerts(), set())

    def test_400_does_not_fail_over(self):
        self.claude.mode = "400"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (400, "cloud-claude"))
        self.assertIn("at least one message", r.json()["error"]["message"])
        self.assertEqual((len(self.openai.requests), len(self.local.requests)), (0, 0))
        self.assertEqual(self.light("answer"), "green", "a bad request isn't the target's fault")

    def test_refusal_is_returned_not_shopped_around(self):
        self.claude.mode = "refuse"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-claude"))
        self.assertEqual(r.json()["choices"][0]["finish_reason"], "content_filter")
        self.assertEqual(len(self.openai.requests), 0)

    def test_stream_breaks_mid_answer_no_splice(self):
        self.claude.mode = "break"
        self.start()
        r = self.ask(stream=True)
        self.assertEqual((r.status, r.target), (200, "cloud-claude"))
        events = r.events()
        self.assertNotIn("[DONE]", events)
        self.assertEqual(json.loads(events[-1])["error"]["code"], "stream_broken")
        self.assertEqual(r.text(), "hello ")
        self.assertEqual((len(self.openai.requests), len(self.local.requests)), (0, 0))
        r = self.ask()
        self.assertEqual((r.status, r.json()["error"]["code"]), (502, "stream_broken"))
        self.assertEqual(self.log_lines(at_least=2)[-1]["outcome"], "stream_broken")

    def test_every_target_down(self):
        for s in (self.claude, self.openai, self.local):
            s.stop()
        self.start()
        t0 = time.monotonic()
        r = self.ask()
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_model"))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(self.light("answer"), "red")

    def test_cooldown_then_retry(self):
        self.raw["cooldown_s"] = 1
        self.claude.mode = "500"
        self.start()
        self.assertEqual(self.ask().target, "cloud-openai")
        self.assertEqual(self.ask().target, "cloud-openai")
        self.assertEqual(len(self.claude.requests), 1, "skipped during the cooldown")
        self.claude.mode = "ok"
        time.sleep(1.1)
        self.assertEqual(self.ask().target, "cloud-claude")
        self.assertEqual(self.light("answer"), "green")

    def test_every_model_sitting_out_is_tried_anyway(self):
        """Rene 10/15: when every model in a lineup is sitting out its cooldown, try them anyway instead of
        answering that no model can. An internet blip takes out a cloud-to-cloud lineup for one question only."""
        self.raw["lineups"]["answer"]["models"] = ["cloud-claude", "cloud-openai"]
        self.claude.mode = self.openai.mode = "500"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_model"))
        self.claude.mode = self.openai.mode = "ok"  # the blip is over; the 60 s cooldown isn't
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-claude"), "tried anyway, in the lineup's order")
        self.assertEqual(self.light("answer"), "green")

    def test_when_the_primary_fails_now_a_model_in_its_cooldown_gets_its_60_seconds(self):
        """Rene 10/15: "give it its 60 seconds before retrying". #1 was up and fails on this request while #2 is
        sitting out: #2 isn't tried, and a lineup of just the two answers that no model can."""
        self.raw["lineups"]["answer"]["models"] = ["cloud-claude", "cloud-openai"]
        self.openai.mode = "500"
        self.start()
        self.assertEqual(self.ask("gpt-5.5", key="owner-desk").status, 503)  # #2 fails and sits out
        asked = len(self.openai.requests)
        self.claude.mode, self.openai.mode = "500", "ok"
        r = self.ask()
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_model"))
        self.assertEqual(len(self.openai.requests), asked, "#2 kept its cooldown")

    def test_when_the_primary_fails_now_the_next_model_that_is_up_answers(self):
        self.openai.mode = "500"
        self.start()
        self.assertEqual(self.ask("gpt-5.5", key="owner-desk").status, 503)  # #2 sits out
        self.claude.mode = "500"
        r = self.ask()  # answer: claude (fails now), openai (sitting out, skipped), local
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))

    def test_held_out_but_testable_is_tried_last(self):
        """Rene 10/15: "test when testable". A local AI app that isn't answering, or lists another model, is held
        out until its check passes; with nothing else left, a real question is the test."""
        self.raw["lineups"]["answer"]["models"] = ["cloud-claude", "local-qwen27"]
        self.local.listed = ["qwen3.6-27b-q8_0"]  # held out: the app lists another model
        self.claude.mode = "500"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "local-qwen27"), "the held-out model, tried last")

    def test_no_api_key_is_not_tried(self):
        """Rene 10/15: "no api is not testable"."""
        self.raw["lineups"]["answer"]["models"] = ["cloud-claude", "cloud-openai"]
        self.target("cloud-openai")["api_key"] = "secret:openai"  # named, never entered
        self.claude.mode = "500"
        self.start()
        r = self.ask()
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_model"))
        self.assertEqual(self.openai.requests, [])

    def test_queue_full_429(self):
        self.target("local-qwen27").update(max_parallel=1, max_queue=0)
        self.local.hold_s = 1.0
        self.start()
        first = []
        th = threading.Thread(target=lambda: first.append(self.ask("summarize")))
        th.start()
        time.sleep(0.3)
        r = self.ask("summarize")
        th.join()
        self.assertEqual((r.status, r.headers.get("Retry-After")), (429, "5"))
        self.assertEqual(first[0].status, 200)

    def test_queue_full_moves_to_next_target(self):
        self.claude.mode = "500"
        self.target("cloud-openai").update(max_parallel=1, max_queue=0)
        self.openai.hold_s = 1.0
        self.start()
        th = threading.Thread(target=self.ask)
        th.start()
        time.sleep(0.3)
        r = self.ask()
        th.join()
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))


class Lanes(unittest.TestCase):
    def test_batch_waits_while_interactive_waits(self):
        gate = routerlm.Gate(1, 10)
        gate.acquire("interactive", time.monotonic() + 1)  # the slot is taken
        order = []

        def take(lane, delay):
            time.sleep(delay)
            gate.acquire(lane, time.monotonic() + 5)
            order.append(lane)
            time.sleep(0.05)
            gate.release()

        batch = threading.Thread(target=take, args=("batch", 0))
        inter = threading.Thread(target=take, args=("interactive", 0.1))
        batch.start()
        inter.start()
        time.sleep(0.3)
        gate.release()
        batch.join()
        inter.join()
        self.assertEqual(order, ["interactive", "batch"], "the batch job waited although it asked first")

    def test_no_slot_before_deadline_is_busy(self):
        gate = routerlm.Gate(1, 10)
        gate.acquire("interactive", time.monotonic() + 1)
        with self.assertRaises(routerlm.TargetError) as e:
            gate.acquire("batch", time.monotonic() + 0.1)
        self.assertEqual(e.exception.kind, "busy")


# ---------------------------------------------------------------------------------------------------------
# Section 18, second bullet: block all outbound traffic and run every role

class Floor(Base):
    def test_outbound_blocked_every_role_answers_locally(self):
        hole = BlackHole()  # a firewall that drops packets
        self.addCleanup(hole.close)
        self.target("cloud-claude")["base_url"] = hole.url
        self.target("cloud-openai")["base_url"] = closed_port()  # a firewall that rejects
        self.start()
        for role in ("answer", "summarize", "reason"):
            for stream in (False, True):
                r = self.ask(role, stream=stream)
                self.assertEqual((r.status, r.target, r.location), (200, "local-qwen27", "local"), (role, stream))
                self.assertEqual(r.text().strip(), "hello from qwen3.6-27b-q4_k_m")
        e = self.request("POST", "/v1/embeddings", {"model": "embed", "input": "stage lights"})
        self.assertEqual((e.status, e.target, e.json()["model"]), (200, "local-embed", "nomic-embed-text-v2"))
        lights = {name: r["light"] for name, r in self.status()["lineups"].items()}
        self.assertEqual(lights, {"answer": "yellow", "reason": "yellow", "summarize": "green", "embed": "green"})


# ---------------------------------------------------------------------------------------------------------
# Section 18, third bullet: local-only against a stub cloud that records everything it receives

class LocalOnly(Base):
    def test_cloud_stub_record_stays_empty(self):
        self.raw["lineups"]["private"] = {"data": "local-only", "models": ["local-qwen27"],
                                        "timeout_first_token_s": T}
        self.start()
        cases = [
            ("header local-only", dict(role="answer", data="local-only")),
            ("header local-only, streamed", dict(role="reason", data="local-only", stream=True)),
            ("key ceiling local-only, header cloud-ok", dict(role="answer", key="hr-reports", data="cloud-ok")),
            ("key ceiling local-only, no header", dict(role="reason", key="hr-reports")),
            ("role local-only", dict(role="private")),
        ]
        for name, kw in cases:
            r = self.ask(**kw)
            self.assertEqual((r.status, r.target), (200, "local-qwen27"), name)
        e = self.request("POST", "/v1/embeddings", {"model": "embed", "input": "x"}, key="hr-reports")
        self.assertEqual(e.target, "local-embed")
        r = self.ask("claude-opus-5-5", key="owner-desk", data="local-only")  # a named cloud model
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "no_local_model"))

        self.local.stop()  # every local target down
        time.sleep(0.3)
        for kw in (dict(role="answer", data="local-only"), dict(role="answer", key="hr-reports"),
                   dict(role="private"), dict(role="answer", data="local-only", stream=True)):
            r = self.ask(**kw)
            self.assertEqual(r.status, 503, kw)
            self.assertEqual(r.json()["error"]["code"], "no_local_model", kw)
            self.assertIn("nothing was sent outside the building", r.json()["error"]["message"])

        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.openai.requests, [])
        # The control: the same stubs do record a cloud-ok request, so the empty record means something.
        self.assertEqual(self.ask("answer").target, "cloud-claude")
        self.assertEqual(len(self.claude.requests), 1)


# ---------------------------------------------------------------------------------------------------------
# Config refusals (section 15)

class ConfigChecks(unittest.TestCase):
    def setUp(self):
        os.environ["RLM_TEST_ANTHROPIC"] = "k"
        os.environ["RLM_TEST_OPENAI"] = "k"
        self.raw = sketch("https://api.anthropic.com/v1", "https://api.openai.com/v1", "http://127.0.0.1:8081/v1",
                          "http://127.0.0.1:8082/v1", "/tmp/unused")

    def refused(self, needle):
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_config(self.raw, resolve_keys=True)
        self.assertIn(needle, str(e.exception))

    def test_sketch_is_accepted(self):
        cfg = routerlm.load_config(self.raw)
        self.assertEqual(list(cfg.roles), ["answer", "summarize", "reason", "embed"])

    def test_unknown_target(self):
        self.raw["lineups"]["answer"]["models"].insert(0, "cloud-gemini")
        self.refused("names model 'cloud-gemini', which doesn't exist")

    def test_any_order_any_length(self):
        """DECISIONS v0.16 (Rene): a lineup ends where its owner decides. No refusal, no warning."""
        for order in (["cloud-claude", "cloud-openai"], ["cloud-openai"], ["local-qwen27", "cloud-claude"]):
            self.raw["lineups"]["answer"]["models"] = order
            cfg = routerlm.load_config(self.raw)
            self.assertEqual([t.id for t in cfg.roles["answer"].targets], order)

    def test_local_only_role_with_cloud_target(self):
        self.raw["lineups"]["answer"]["data"] = "local-only"
        self.refused("is local only but lists cloud model 'cloud-claude'")

    def test_local_target_outside_private_space(self):
        self.raw["models"][2]["base_url"] = "http://8.8.8.8:8081/v1"
        self.refused("is marked local but '8.8.8.8' is outside private address space")

    def test_embed_with_two_targets(self):
        self.raw["lineups"]["embed"]["models"] = ["local-embed", "local-qwen27"]
        self.refused("an embedding lineup has exactly one model")

    def test_embed_on_cloud(self):
        self.raw["lineups"]["embed"] = {"data": "cloud-ok", "models": ["cloud-openai"]}
        self.refused("the embedding model must be local")

    def test_listen_on_every_interface(self):
        self.raw["listen"] = "0.0.0.0:8080"
        self.refused("listen on the LAN interface's own address")

    def test_missing_provider_key_is_out_of_use_not_a_refused_start(self):
        del os.environ["RLM_TEST_OPENAI"]
        t = routerlm.load_config(self.raw).targets["cloud-openai"]
        self.assertTrue(t.is_down())
        self.assertIn("environment variable RLM_TEST_OPENAI is empty", t.state()["reason"])

    def test_keychain_off_macos_is_a_plain_problem(self):
        def no_security(*args, **kw):
            raise FileNotFoundError(2, "No such file or directory", "security")
        real = routerlm.subprocess.run
        routerlm.subprocess.run = no_security  # what Linux does: there is no `security` command
        self.addCleanup(setattr, routerlm.subprocess, "run", real)
        self.raw["models"][0]["api_key"] = "keychain:routerlm-anthropic"
        t = routerlm.load_config(self.raw).targets["cloud-claude"]
        self.assertTrue(t.is_down())
        self.assertIn("keychain: works on macOS only", t.state()["reason"])

    def test_every_problem_listed_at_once(self):
        self.raw["lineups"]["answer"]["models"] = ["cloud-claude", "cloud-gemini"]
        self.raw["listen"] = "0.0.0.0:8080"
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_config(self.raw)
        self.assertEqual(str(e.exception).count("\n  - "), 2)

    def test_example_config(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.example.json")
        with open(path) as f:
            raw = json.load(f)
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_config(raw, resolve_keys=False)
        self.assertIn("key_sha256 must be 64 lowercase hex", str(e.exception), "placeholders must be replaced")
        for c in raw["apps"].values():
            c["key_sha256"] = h("x")
        routerlm.load_config(raw, resolve_keys=False)

    def test_an_empty_config_starts(self):
        cfg = routerlm.load_config(routerlm.STARTER, "/tmp/unused")
        self.assertEqual((cfg.roles, cfg.targets, cfg.callers, cfg.admin_password), ({}, {}, {}, None))


# ---------------------------------------------------------------------------------------------------------
# The front door: translation, labels, auth, limits, the log (sections 3, 6, 12, 14)

class FrontDoor(Base):
    def test_anthropic_request_translation(self):
        self.start()
        tools = [{"type": "function", "function": {"name": "lookup", "description": "Find a quote",
                                                   "parameters": {"type": "object",
                                                                  "properties": {"order": {"type": "string"}}}}}]
        self.ask(tools=tools, tool_choice="required", stop="END")
        path, headers, body = self.claude.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers.get("x-api-key"), "sk-ant-test")
        self.assertEqual(body["system"], "Answer from the records.")
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}])
        self.assertEqual((body["model"], body["max_tokens"], body["stream"]), ("claude-opus-5-5", 32000, True))
        self.assertNotIn("temperature", body)
        self.assertEqual(body["stop_sequences"], ["END"])
        self.assertEqual(body["tools"][0]["input_schema"]["properties"], {"order": {"type": "string"}})
        self.assertEqual(body["tool_choice"], {"type": "auto"})

    def test_tool_call_round_trip(self):
        self.claude.mode = "tool"
        self.start()
        msg = self.ask().json()["choices"][0]
        self.assertEqual(msg["finish_reason"], "tool_calls")
        call = msg["message"]["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["name"]), ("toolu_1", "lookup"))
        self.assertEqual(json.loads(call["function"]["arguments"]), {"order": "Q-1042"})
        self.claude.mode = "ok"
        history = [{"role": "user", "content": "Look up Q-1042"},
                   {"role": "assistant", "content": None, "tool_calls": [call]},
                   {"role": "tool", "tool_call_id": "toolu_1", "content": "Q-1042: $4,200"}]
        self.request("POST", "/v1/chat/completions", {"model": "answer", "messages": history})
        sent = self.claude.requests[-1][2]["messages"]
        self.assertEqual(sent[1]["content"][0], {"type": "tool_use", "id": "toolu_1", "name": "lookup",
                                                 "input": {"order": "Q-1042"}})
        self.assertEqual(sent[2]["content"][0]["type"], "tool_result")

    def test_openai_tool_call_assembled(self):
        self.claude.mode = "500"
        self.openai.mode = "tool"
        self.start()
        call = self.ask().json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"order": "Q-1042"})

    def test_stream_labels_and_usage(self):
        self.start()
        r = self.ask(stream=True)
        chunks = [json.loads(e) for e in r.events() if e != "[DONE]"]
        self.assertEqual({c["model"] for c in chunks}, {"claude-opus-5-5"})
        self.assertFalse([c for c in chunks if not c["choices"]], "no usage chunk unless asked")
        self.assertEqual(r.events()[-1], "[DONE]")
        r = self.ask(stream=True, stream_options={"include_usage": True})
        usage = [json.loads(e) for e in r.events() if e != "[DONE]" and not json.loads(e)["choices"]]
        self.assertEqual(usage[0]["usage"]["completion_tokens"], 5)

    def test_log_line_has_no_prompt_text(self):
        self.claude.mode = "500"
        self.start()
        self.ask()
        line = self.log_lines()[-1]
        self.assertEqual({k: line[k] for k in ("app", "lineup", "data", "model", "location", "outcome",
                                               "failover_from")},
                         {"app": "capsule", "lineup": "answer", "data": "cloud-ok", "model": "cloud-openai",
                          "location": "cloud", "outcome": "ok", "failover_from": "cloud-claude"})
        self.assertEqual((line["in"], line["out"]), (10, 3))
        for name in os.listdir(self.tmp):
            with open(os.path.join(self.tmp, name)) as f:
                self.assertNotIn("PRIVATE-MARKER-7731", f.read())

    def test_debug_switch(self):
        self.raw["debug"] = True
        self.start()
        self.ask()
        self.assertTrue(self.status()["debug"])
        deadline, text = time.monotonic() + 2, ""  # written after the answer is sent, like the log line
        while "PRIVATE-MARKER-7731" not in text and time.monotonic() < deadline:
            time.sleep(0.02)
            for name in os.listdir(self.tmp):
                if name.startswith("debug-"):
                    with open(os.path.join(self.tmp, name)) as f:
                        text = f.read()
        self.assertIn("PRIVATE-MARKER-7731", text)

    def test_auth_and_naming(self):
        self.start()

        r = self.ask("qwen3.6-27b-q4_k_m")
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "concrete_model_not_allowed"))
        r = self.ask("qwen3.6-27b-q4_k_m", key="owner-desk")
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))
        self.assertEqual(self.ask("nope").status, 404)
        ids = [m["id"] for m in self.request("GET", "/v1/models").json()["data"]]
        self.assertEqual(ids, ["answer", "summarize", "reason", "embed"])
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"model": "embed", "messages": []}).status, 400)
        self.assertEqual(self.ask(data="sideways").status, 400)
        self.as_other_computer()
        self.assertEqual(self.ask(key=None).status, 401, "another computer needs an app's API key")
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"model": "answer"}, headers={
            "Authorization": "Bearer nope"}).status, 401)

    def test_apps_on_this_mac_need_no_key(self):
        """Rene 10/15: "apps on the RouterLM Mac need no key, and the page lists them as 'this Mac' unless they send
        a name". The name is whatever is in the app's API key box."""
        self.start()
        self.assertEqual(self.ask(key=None).status, 200)
        r = self.request("POST", "/v1/chat/completions", {"model": "answer", "messages": [
            {"role": "user", "content": "hi"}]}, key=None, headers={"Authorization": "Bearer FileMaker"})
        self.assertEqual(r.status, 200)
        r = self.request("POST", "/v1/chat/completions", {"model": "answer", "messages": [
            {"role": "user", "content": "hi"}]}, key=None, headers={"Authorization": "Bearer sk-ant-SECRET-4321"})
        self.assertEqual(r.status, 200)
        self.assertEqual([x["app"] for x in self.log_lines(at_least=3)][-3:], ["this Mac", "filemaker", "this Mac"])
        page = self.page().raw.decode()
        self.assertIn("<td>filemaker</td>", page)
        self.assertNotIn("SECRET", page + "".join(self.file(n) for n in os.listdir(self.tmp)
                                                  if n.startswith("routerlm-")), "a key is never shown as a name")
        r = self.request("POST", "/v1/chat/completions", {"model": "answer", "messages": [
            {"role": "user", "content": "hi"}]}, key=None, headers={"Authorization": "Bearer hr-reports"})
        self.assertEqual((r.status, r.target), (200, "local-qwen27"), "the settings of the app by that name apply")
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"model": "answer"}, key=None, headers={
            "Origin": "http://evil.example"}).status, 403, "a web page from another site needs a key")
        self.assertEqual(self.raw_request("POST", "/v1/chat/completions", {"Host": f"rebound.example:{self.port}"},
                                          b'{"model": "answer"}').status, 403, "a page that rebinds its name")

    def file(self, name):
        with open(os.path.join(self.tmp, name)) as f:
            return f.read()

    def test_max_context_refused_not_truncated(self):
        self.raw["lineups"]["answer"]["max_context"] = 100
        self.start()
        r = self.ask(messages=[{"role": "user", "content": "x" * 2000}])
        self.assertEqual((r.status, r.json()["error"]["code"]), (400, "context_length_exceeded"))
        self.assertEqual(self.claude.requests, [])

    def test_embedding_down_fails_fast_no_fallback(self):
        self.embedder.stop()
        self.start()
        t0 = time.monotonic()
        r = self.request("POST", "/v1/embeddings", {"model": "embed", "input": "x"})
        self.assertEqual(r.status, 503)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual((self.claude.requests, self.openai.requests, self.local.requests), ([], [], []))


# ---------------------------------------------------------------------------------------------------------
# The weights hash pin (section 6)

class WeightsPin(Base):
    def setUp(self):
        super().setUp()
        self.model_file = os.path.join(self.tmp, "qwen.gguf")
        self.write_model(b"GGUF weights A")
        self.target("local-qwen27")["weights_sha256"] = hashlib.sha256(b"GGUF weights A").hexdigest()
        self.local.props = {"model_path": self.model_file, "total_slots": 2}  # llama-server's GET /props

    def write_model(self, data):
        with open(self.model_file, "wb") as f:
            f.write(data)

    def test_pinned_file_answers(self):
        self.start()
        r = self.ask("summarize")
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))
        self.assertEqual(self.light("summarize"), "green")

    def test_out_of_use_until_hashed(self):
        t = routerlm.load_config(self.raw, self.tmp).targets["local-qwen27"]
        self.assertTrue(t.is_down())
        self.assertIn("checking the model file's fingerprint", t.state()["reason"])

    def test_file_replaced_refused_and_alerted_until_restored(self):
        self.start()
        self.assertEqual(self.ask("summarize").status, 200)
        self.write_model(b"GGUF weights B, a newer quantization")  # downloaded over the pinned file
        time.sleep(0.4)
        r = self.ask("summarize")  # nothing else in the lineup: "try it anyway" (Rene)
        self.assertEqual((r.status, r.target), (200, "local-qwen27"))
        self.assertEqual(self.light("summarize"), "red", "still held out after answering")
        self.assertIn(("local-qwen27", "model file changed"), self.alerts())
        self.assertEqual(self.light("answer"), "green", "a cloud-first role still has its first target")
        self.write_model(b"GGUF weights A")
        time.sleep(0.4)
        self.assertEqual(self.ask("summarize").status, 200)
        self.assertEqual(self.alerts(), set())

    def test_hashed_once_per_change(self):
        calls, real = [], routerlm.hash_file
        routerlm.hash_file = lambda path: calls.append(path) or real(path)
        self.addCleanup(setattr, routerlm, "hash_file", real)
        self.start()
        time.sleep(0.5)  # about five health passes
        self.assertEqual(len(calls), 1)
        self.write_model(b"GGUF weights B, a newer quantization")
        time.sleep(0.5)
        self.assertEqual(len(calls), 2)

    def test_a_failed_check_hands_over_to_the_next_model(self):
        """Rene 10/15: when a model fails its check, the next model in the lineup answers. Nobody is stuck."""
        self.raw["lineups"]["answer"]["models"] = ["local-qwen27", "cloud-openai"]
        self.start()
        self.assertEqual(self.ask().target, "local-qwen27")
        self.write_model(b"GGUF weights B, a newer quantization")  # the pin's check fails from here on
        time.sleep(0.4)
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, "cloud-openai"), "#2 answers while #1 is held out")
        self.assertEqual(self.log_lines(at_least=2)[-1]["model"], "cloud-openai")
        self.assertEqual(self.light("answer"), "yellow")
        alert = next(a for a in self.status()["alerts"] if a["model"] == "local-qwen27")
        self.assertIn("The next model in its lineups answers meanwhile", alert["message"])
        self.write_model(b"GGUF weights A")
        time.sleep(0.4)
        self.assertEqual(self.ask().target, "local-qwen27", "back to #1 once its check passes")
        self.assertEqual(self.light("answer"), "green")

    def test_a_check_still_running_hands_over_too(self):
        """A pinned model is held out until its first check is done; meanwhile the next model answers."""
        self.raw["lineups"]["answer"]["models"] = ["local-qwen27", "cloud-openai"]
        release, real = threading.Event(), routerlm.hash_file
        routerlm.hash_file = lambda path: release.wait(5) and real(path)  # a big file, still hashing
        self.addCleanup(setattr, routerlm, "hash_file", real)
        self.addCleanup(release.set)
        self.start()
        self.assertEqual(self.ask().target, "cloud-openai")
        release.set()
        time.sleep(0.4)
        self.assertEqual(self.ask().target, "local-qwen27")

    def test_engine_that_does_not_say_needs_weights_path(self):
        self.local.props = None  # LM Studio, Ollama: no /props
        self.start()
        self.assertEqual(self.ask("summarize").status, 200, "nothing else left: tried anyway")
        self.assertIn(("local-qwen27", "can't check the model file"), self.alerts())

    def test_weights_path_when_the_engine_does_not_say(self):
        self.local.props = {"model_path": "../models/qwen.gguf"}  # relative, as llama-server repeats -m
        self.target("local-qwen27")["weights_path"] = self.model_file
        self.start()
        self.assertEqual((self.ask("summarize").status, self.alerts()), (200, set()))

    def test_config_refusals(self):
        self.target("local-qwen27")["weights_sha256"] = "9f2c"
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_config(self.raw, self.tmp)
        self.assertIn("weights_sha256 must be 64 lowercase hex", str(e.exception))
        self.target("local-qwen27")["weights_sha256"] = h("x")
        self.target("cloud-openai")["weights_sha256"] = h("x")
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_config(self.raw, self.tmp)
        self.assertIn("pins the model file of a local AI app", str(e.exception))


# ---------------------------------------------------------------------------------------------------------
# The admin page (sections 3, 12, 14, 16; APPLIANCE.md "Admin page")

class AdminPage(Base):
    def test_open_on_this_machine_password_for_others(self):
        self.start()
        r = self.page()
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "text/html; charset=utf-8"), "no password here")
        r = self.raw_request("GET", "/", {"Host": f"rebound.example:{self.port}"})
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "bad_host"), "a page that rebinds its name")
        self.as_other_computer()
        r = self.page()
        self.assertEqual(r.status, 401)
        self.assertTrue(r.headers.get("WWW-Authenticate", "").startswith("Basic "), "the browser asks for it")
        self.assertEqual(self.page(f"Bearer {KEYS['capsule']}").status, 401, "a caller key isn't the password")
        self.assertEqual(self.page(self.basic("wrong")).status, 401)
        self.assertEqual(self.page(self.basic()).status, 200)
        self.assertEqual(self.page(f"Bearer {KEYS['admin']}").status, 200)

    def test_other_computers_shut_out_until_a_password_is_set(self):
        del self.raw["admin_password"]
        self.start()
        self.assertEqual(self.page().status, 200)
        self.as_other_computer()
        r = self.page(self.basic())
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "admin_off"))

    def test_lights_activity_usage_and_no_prompt_text(self):
        self.claude.mode = "500"
        self.start()
        self.ask()
        self.log_lines()
        page = self.page(self.basic()).raw.decode()
        for needle in ("<span class='dot yellow'></span>yellow</td><td><b>answer</b>", "<b>summarize</b>",
                       "<td>gpt-5.5 (cloud)</td><td>answered</td>", "<td>claude-opus-5-5 (cloud)</td></tr>",
                       "<td>capsule</td><td>1</td><td>10</td><td>3</td>", "memory in use", "disk free"):
            self.assertIn(needle, page)
        self.assertNotIn("PRIVATE-MARKER-7731", page)

    def test_alerts_and_debug_banner(self):
        self.claude.mode = "401"
        self.raw["debug"] = True
        self.start()
        self.ask()
        page = self.page(self.basic()).raw.decode()
        self.assertIn("<span class=bad>API key rejected</span>", page)
        self.assertIn("Troubleshooting capture is on", page)

    def test_status_says_when_each_role_last_answered(self):
        self.start()
        self.assertIsNone(self.status()["lineups"]["answer"]["last_answer"])
        self.ask()
        self.log_lines()
        self.assertIsNotNone(self.status()["lineups"]["answer"]["last_answer"])
        self.assertIsNone(self.status()["lineups"]["summarize"]["last_answer"])

    def test_restart_keeps_the_page(self):
        self.start()
        self.ask()
        self.log_lines()
        router = routerlm.Router(routerlm.load_config(self.raw, self.tmp))  # a fresh start reads the log
        self.assertEqual(len(router.recent), 1)
        self.assertIsNotNone(router.cfg.roles["answer"].last_answer)
        self.assertEqual([c[0] for c in router.daily.values()], [1])

    def test_log_lines_from_before_the_rename_still_count(self):
        old = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "caller": "capsule", "role": "answer",
               "target": "cloud-claude", "location": "cloud", "in": 7, "out": 2, "outcome": "ok"}
        with open(os.path.join(self.tmp, f"routerlm-{time.strftime('%Y-%m', time.gmtime())}.jsonl"), "w") as f:
            f.write(json.dumps(old) + "\n")
        self.start()
        page = self.page().raw.decode()
        self.assertIn("<td>capsule</td><td>1</td><td>7</td><td>2</td>", page)
        self.assertIsNotNone(self.status()["lineups"]["answer"]["last_answer"])

    def test_each_lineup_shows_its_order_and_the_primary(self):
        self.start()
        page = self.page().raw.decode()
        self.assertIn("<b>#1 Primary</b> claude-opus-5-5 (cloud) · <span class=muted>not asked yet</span><br>"
                      "<span class='dot grey'></span><b>#2</b> gpt-5.5 (cloud) · <span class=muted>not asked yet"
                      "</span><br><span class='dot green'></span><b>#3</b> qwen3.6-27b-q4_k_m (local) · "
                      "<span class=muted>ready, not asked yet</span>", page)
        settings = self.raw_request("GET", "/settings", {}).raw.decode()
        self.assertIn("<label>#1 Primary<select name=t0>", settings)
        self.assertIn("<label>#2<select name=t1>", settings)

    def test_each_model_gets_a_dot_and_red_stays_until_it_answers(self):
        """Rene 10/15 ("yes" to Claude's items 1-3): a dot per model in each lineup row, from what RouterLM already
        knows. A model that failed stays red after its cooldown, until it answers again."""
        self.raw["cooldown_s"] = 0.3
        self.claude.mode = "500"
        self.start()
        self.assertEqual(self.ask().target, "cloud-openai")
        self.assertEqual(self.dots("answer"), ["red", "green", "green"])
        self.assertIn("<span class='dot red'></span><b>#1 Primary</b> claude-opus-5-5 (cloud) · <span class=bad>down"
                      "</span> <span class=muted>HTTP 529", self.page().raw.decode(), "no short name in front")
        time.sleep(0.4)  # the cooldown is over: it would be tried again, but its last try failed
        self.assertEqual(self.dots("answer")[0], "red")
        page = self.page().raw.decode()
        self.assertIn("claude-opus-5-5 (cloud) · <span class=bad>failed ", page)
        self.assertIn("gpt-5.5 (cloud) · <span class=muted>answered ", page)
        first = self.status()["lineups"]["answer"]["models"][0]
        self.assertIn("cloud-claude: HTTP 529 overloaded", first["last_failure"])
        self.claude.mode = "ok"
        self.assertEqual(self.ask().target, "cloud-claude")
        self.assertEqual(self.dots("answer"), ["green", "green", "green"])

    def test_every_failed_try_counts_not_only_the_last(self):
        """The log keeps only the last model that failed before an answer; the dots record every one."""
        self.raw["cooldown_s"] = 0.3
        self.claude.mode = self.openai.mode = "500"
        self.start()
        self.assertEqual(self.ask().target, "local-qwen27")
        self.assertEqual(self.log_lines()[-1]["failover_from"], "cloud-openai")
        time.sleep(0.4)  # both cooldowns over
        self.assertEqual(self.dots("answer"), ["red", "red", "green"])

    def test_an_unasked_cloud_backup_is_grey(self):
        self.start()
        self.assertEqual(self.ask().target, "cloud-claude")
        self.assertEqual(self.dots("answer"), ["green", "grey", "green"], "the local one is ready; nobody asked #2")

    def test_the_dots_come_back_after_a_restart(self):
        self.raw["cooldown_s"] = 0.3
        self.claude.mode = "500"
        self.start()
        self.ask()
        self.log_lines()
        router = routerlm.Router(routerlm.load_config(self.raw, self.tmp))  # a fresh start reads the log
        lights = {tid: t.state()["light"] for tid, t in router.cfg.targets.items()}
        self.assertEqual((lights["cloud-claude"], lights["cloud-openai"]), ("red", "green"))

    def test_the_pages_speak_plain_english(self):
        """Rene 10/15: "an angineer is not my primary persona". The words below meant nothing to the would-be
        builder or the owner, so they stay off the pages (the settings file at the bottom is for engineers)."""
        self.raw["debug"] = True
        self.start()
        self.ask()
        self.log_lines()
        settings = self.raw_request("GET", "/settings", {}).raw.decode()
        settings = re.sub(r"<textarea name=raw.*?</textarea>", "", settings, flags=re.S)
        pages = re.sub(r"<style>.*?</style>|<script>.*?</script>|<[^>]+>", " ",
                       self.page().raw.decode() + settings, flags=re.S)
        for word in ("Provider", "Roles", "Callers", "Speaks", "Lane", "Interactive", "First-token", "Listen on",
                     "Cooldown", "Debug", "Pinned", "hash", "Engine", "target", "Outcome", "Requests at once",
                     "Gate", "safe miss", "config"):
            self.assertNotIn(word, pages, word)
        for words in ("Priority", "Which models", "Move on if no answer starts within", "Troubleshooting",
                      "File fingerprint", "Questions and tokens per day", "#1 Primary"):
            self.assertIn(words, pages, "the check reads the page text")


# ---------------------------------------------------------------------------------------------------------
# The check set (section 13)

ANSWERS = {  # what the model under test says, by question
    "quote for the stage lights": "Q-1042 quoted $4,200 for 12 PAR cans.",
    "delivery included": "Yes, delivery is included.",
    "due back": "I don't know; the records don't give a return date.",
}


def answer_by_question(answers):
    def reply(body):
        content = body["messages"][-1]["content"]
        return next((v for k, v in answers.items() if k in content), "No idea.")
    return reply


def stub_grader(body):
    """Grades like a careful model would on these three questions. body is an Anthropic request."""
    prompt = "".join(b.get("text", "") for b in body["messages"][-1]["content"])
    answer = prompt.split("<<<\n")[1].split("\n>>>")[0]
    grade = ("safe_miss" if "don't know" in answer else "unsafe_miss" if "delivery is included" in answer
             else "pass")
    return json.dumps({"grade": grade, "reason": "stub grader"})


def check_set(**changes):
    cs = {"name": "test set", "data": "cloud-ok", "grader": "cloud-claude", "max_safe_misses": 1,
          "system": "Answer from the records.",
          "questions": [
              {"id": "lights", "context": "Records:\nQ-1042: 12 PAR cans, $4,200, customer picks up.",
               "ask": "What did Q-1042 quote for the stage lights?", "must_contain": ["$4,200"],
               "must_not_claim": ["delivery is included"]},
              {"id": "delivery", "ask": "Is delivery included on Q-1042?", "must_contain": ["no"],
               "must_not_claim": ["delivery is included"]},
              {"id": "due", "ask": "When is the Hendricks order due back?",
               "must_contain": ["no return date in the records"]}]}
    cs.update(changes)
    return cs


def verdicts(cfg, run):
    return {v for _, _, v in routerlm.gates(cfg, run)}


class CheckSet(Base):
    def setUp(self):
        super().setUp()
        self.answers = dict(ANSWERS)
        self.local.reply_fn = self.openai.reply_fn = answer_by_question(self.answers)
        self.claude.reply_fn = stub_grader

    def write(self, cs):
        os.makedirs(os.path.join(self.tmp, "test-questions"), exist_ok=True)
        with open(os.path.join(self.tmp, "test-questions", "questions.json"), "w") as f:
            json.dump(cs, f)

    def run_on(self, tid, cs=None):
        self.write(cs or check_set())
        cfg = routerlm.load_config(self.raw, self.tmp)
        return cfg, routerlm.run_check_set(cfg, routerlm.load_check_set(cfg), cfg.targets[tid])[1]

    def test_grades_and_gates(self):
        cfg, run = self.run_on("local-qwen27")
        self.assertEqual(routerlm.tally(run), {"pass": 1, "safe_miss": 1, "unsafe_miss": 1, "error": 0,
                                               "ungraded": 0})
        self.assertEqual(routerlm.gates(cfg, run), [("answer", "backup", "fails"), ("summarize", "primary", "fails"),
                                                    ("reason", "backup", "fails")])
        sent = self.local.requests[0][2]["messages"]  # the check set's system prompt, the records, the question
        self.assertEqual(sent[0], {"role": "system", "content": "Answer from the records."})
        self.assertTrue(sent[1]["content"].startswith("Records:\nQ-1042"))
        self.assertTrue(sent[1]["content"].endswith("What did Q-1042 quote for the stage lights?"))
        prompt = self.claude.requests[0][2]["messages"][0]["content"][0]["text"]  # the grader gets answer and key
        self.assertIn("Q-1042 quoted $4,200 for 12 PAR cans.", prompt)
        self.assertIn("Required facts:\n- $4,200", prompt)
        run["finished"] = None
        self.assertEqual(verdicts(cfg, run), {"run didn't finish"})

    def test_backup_gate_allows_safe_misses_first_needs_every_pass(self):
        self.answers["delivery included"] = "No, the customer picks up."
        cfg, run = self.run_on("local-qwen27")
        self.assertEqual(routerlm.gates(cfg, run), [("answer", "backup", "passes"), ("summarize", "primary", "fails"),
                                                    ("reason", "backup", "passes")])
        self.answers["due back"] = "There's no return date in the records."
        cfg, run = self.run_on("local-qwen27")
        self.assertEqual(verdicts(cfg, run), {"passes"})

    def test_local_only_keeps_the_questions_in(self):
        self.write(check_set(data="local-only"))
        cfg = routerlm.load_config(self.raw, self.tmp)
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_check_set(cfg)
        self.assertIn("the grader has to be local too", str(e.exception))
        self.write(check_set(data="local-only", grader=None))
        cs = routerlm.load_check_set(cfg)
        self.assertIn("is cloud", routerlm.check_refusal(cfg, cs, "cloud-openai"))
        self.assertIn("serves embeddings", routerlm.check_refusal(cfg, cs, "local-embed"))
        self.assertIsNone(routerlm.check_refusal(cfg, cs, "local-qwen27"))
        self.assertEqual((self.claude.requests, self.openai.requests), ([], []))

    def test_no_failover_inside_a_run(self):
        self.local.stop()
        cfg, run = self.run_on("local-qwen27")
        self.assertEqual(routerlm.tally(run)["error"], 3)
        self.assertEqual((self.openai.requests, self.claude.requests), ([], []), "nobody else answered")
        self.assertEqual(verdicts(cfg, run), {"fails"})

    def test_target_in_no_role_yet(self):
        self.raw["models"].append({"id": "local-new", "location": "local", "adapter": "openai",
                                    "base_url": self.local.url, "model": "qwen3.6-35b-q4_k_m"})
        cfg, run = self.run_on("local-new")
        self.assertEqual((len(run["results"]), routerlm.gates(cfg, run)), (3, []))

    def test_grader_without_a_grade(self):
        self.claude.reply_fn = lambda body: "Looks fine to me."
        cfg, run = self.run_on("local-qwen27")
        self.assertEqual(routerlm.tally(run)["ungraded"], 3)
        self.assertEqual(verdicts(cfg, run), {"not graded yet"})

    def test_a_person_grades(self):
        cfg, _ = self.run_on("local-qwen27", check_set(grader=None))
        path, said, replies = routerlm.run_files(cfg)[0], [], iter(["p", "", "s", "u"])  # Enter can't keep ungraded
        run = routerlm.grade_cli(cfg, path, ask=lambda _: next(replies), say=said.append)
        self.assertEqual([r["grade"] for r in run["results"]], ["pass", "safe_miss", "unsafe_miss"])
        self.assertEqual({r["graded_by"] for r in run["results"]}, {"person"})
        self.assertIsNotNone(run["reviewed"])
        with open(path) as f:
            self.assertEqual(json.load(f)["reviewed"], run["reviewed"])
        self.assertIn("Q-1042 quoted $4,200 for 12 PAR cans.", "\n".join(said))

    def test_a_person_reviews_the_model_grading(self):
        cfg, _ = self.run_on("local-qwen27")
        replies = iter(["", "", "u"])  # keep, keep, safe miss -> unsafe miss
        run = routerlm.grade_cli(cfg, routerlm.run_files(cfg)[0], ask=lambda _: next(replies), say=lambda _: None)
        self.assertEqual([r["graded_by"] for r in run["results"]], ["cloud-claude", "cloud-claude", "person"])
        self.assertEqual(routerlm.tally(run)["unsafe_miss"], 2)
        self.assertIsNotNone(run["reviewed"])

    def test_a_refused_file_can_still_be_checked(self):
        """Section 6: the new file is checked before its hash goes in the config."""
        new = os.path.join(self.tmp, "new.gguf")
        with open(new, "wb") as f:
            f.write(b"new weights")
        self.local.props = {"model_path": new}
        self.target("local-qwen27")["weights_sha256"] = h("old weights")
        cfg, run = self.run_on("local-qwen27")
        digest = hashlib.sha256(b"new weights").hexdigest()
        self.assertEqual(run["weights"], {"file": new, "sha256": digest, "pinned": h("old weights")})
        self.assertEqual(routerlm.tally(run)["error"], 0)
        self.assertIn(f"put {digest} in the model's File fingerprint", routerlm.weights_note(run["weights"]))

    def test_check_set_refusals_listed_at_once(self):
        cs = check_set()
        del cs["max_safe_misses"]
        cs["questions"] += [{"id": "lights", "ask": "Again?", "must_contain": ["x"]}, {"id": "empty"}]
        self.write(cs)
        with self.assertRaises(routerlm.ConfigError) as e:
            routerlm.load_check_set(routerlm.load_config(self.raw, self.tmp))
        for needle in ("max_safe_misses must be a whole number", "question 'lights': id used twice",
                       "question 'empty': needs ask", "question 'empty': give must_contain"):
            self.assertIn(needle, str(e.exception))

    def test_example_check_set(self):
        os.makedirs(os.path.join(self.tmp, "test-questions"))
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "test-questions.example.json"),
                    os.path.join(self.tmp, "test-questions", "questions.json"))
        cs = routerlm.load_check_set(routerlm.load_config(self.raw, self.tmp))
        self.assertEqual((len(cs["questions"]), cs["data"], cs["grader"]), (3, "local-only", None))

    def test_run_from_the_admin_page(self):
        self.write(check_set())
        self.start()
        self.assertEqual(self.run_button("local-qwen27", origin="http://evil.example").status, 403)
        self.assertEqual(self.run_button("local-embed").status, 400)
        r = self.run_button("local-qwen27")
        self.assertEqual((r.status, r.headers.get("Location")), (303, "/"))
        # (another computer with a caller key instead of the password is refused: Settings tests)
        deadline = time.monotonic() + 10
        while not routerlm.latest_runs(self.srv.router.cfg).get("local-qwen27", {}).get("finished"):
            self.assertLess(time.monotonic(), deadline, "the run didn't finish")
            time.sleep(0.05)
        page = self.page(self.basic()).raw.decode()
        self.assertIn("1 / 1 / <span class=bad>1</span>", page)
        self.assertIn("summarize, as Primary: <span class=bad>fails</span>", page)
        with open(os.path.join(self.tmp, f"routerlm-{time.strftime('%Y-%m', time.gmtime())}.jsonl")) as f:
            events = [json.loads(x) for x in f if '"test_questions"' in x]
        self.assertEqual((events[0]["model"], events[0]["pass"]), ("local-qwen27", 1))
        self.assertNotIn("Q-1042", json.dumps(events), "the log keeps counts, not questions")

    def test_cli(self):
        self.write(check_set())
        path = os.path.join(self.tmp, "routerlm.json")
        with open(path, "w") as f:
            json.dump(self.raw, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(routerlm.main(["test-questions", "--config", path, "--model", "local-qwen27"]), 0)
        self.assertIn("summarize (primary): fails", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(routerlm.main(["test-questions", "--config", path, "--model", "local-embed"]), 2)
        self.assertIn("serves embeddings", err.getvalue())


# ---------------------------------------------------------------------------------------------------------
# The settings page (DECISIONS.md 2026-10-15: set up from a browser, no terminal)

class Settings(Base):
    def post(self, path, fields, auth=None, origin=None):
        return self.raw_request("POST", path, {
            "Authorization": self.basic() if auth is None else auth,
            "Origin": origin or f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(fields).encode())

    def file(self, name="routerlm.json"):
        with open(os.path.join(self.tmp, name)) as f:
            return f.read()

    def settings(self):
        return self.raw_request("GET", "/settings", {"Authorization": self.basic()}).raw.decode()

    def test_first_visit_quick_start_to_a_first_answer(self):
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp, "health_interval_s": 0.1}
        real = routerlm.PRESETS["lmstudio"]
        routerlm.PRESETS["lmstudio"] = real[:3] + (self.local.url,)  # the stub stands in for LM Studio on its port
        self.addCleanup(routerlm.PRESETS.__setitem__, "lmstudio", real)
        self.local.listed = ["qwen3.6-27b-q4_k_m", "text-embedding-nomic-embed-text-v1.5"]
        self.start()
        r = self.page()
        self.assertEqual((r.status, r.headers.get("Location")), (303, "/settings"), "nothing set up: straight to it")
        page = self.settings()
        self.assertIn("Quick start", page)
        pick = f"{self.local.url}|qwen3.6-27b-q4_k_m"
        self.assertIn(f"value='{pick}'", page)
        self.assertNotIn("text-embedding", page.split("Quick start")[1].split("</section>")[0], "no embedders")
        r = self.post("/settings", {"do": "quick_start", "pick": pick})
        self.assertEqual(r.status, 303)
        note = self.raw_request("GET", r.headers["Location"], {}).raw.decode()
        self.assertIn("The answer lineup: #1 Primary qwen3.6-27b-q4_k_m (LM Studio, local).", note)
        self.assertIn("On this Mac the app needs no API key", note)
        r = self.ask(key=None)
        self.assertEqual((r.status, r.location), (200, "local"), "first answer: start, open, pick, ask")
        self.assertNotIn("Quick start", self.settings())
        self.assertEqual(self.page().status, 200)

    def test_admin_link_shows_the_admin_page_before_setup(self):
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp}
        self.start()
        self.assertIn("<a href=/admin>", self.settings())
        r = self.raw_request("GET", "/admin", {})
        self.assertEqual(r.status, 200, "Settings' admin link doesn't bounce back")
        self.assertIn("Not set up yet", r.raw.decode())

    def test_a_key_added_later_brings_its_model_in(self):
        self.target("cloud-claude")["api_key"] = "secret:anthropic"  # named in the config, not entered yet
        self.start()
        self.assertIn(("cloud-claude", "API key missing"), self.alerts())
        self.assertEqual(self.ask().target, "cloud-openai")
        self.assertEqual(self.claude.requests, [], "no request without a key")
        self.post("/settings", {"do": "key_add", "name": "anthropic", "value": "sk-ant-test"})
        self.assertEqual(self.alerts(), set())
        self.assertEqual(self.ask().target, "cloud-claude")

    def test_names_default_or_get_cleaned_never_refused(self):
        self.start()
        for value in ("sk-ant-api03-one", "sk-ant-api03-two", "sk-proj-three", "xyz-four"):
            self.assertEqual(self.post("/settings", {"do": "key_add", "name": "", "value": value}).status, 303)
        self.post("/settings", {"do": "key_add", "name": "My Test Key", "value": "sk-five"})
        self.assertEqual(sorted(json.loads(self.file("secrets.json"))["keys"]),
                         ["anthropic", "anthropic-2", "key", "my-test-key", "openai"])
        self.assertEqual(self.post("/settings", {"do": "caller_add", "name": ""}).status, 200)
        self.assertIn("my-app", json.loads(self.file())["apps"])
        page = self.settings()
        self.assertNotIn("placeholder=anthropic", page, "nothing grey that looks filled in")
        self.assertIn("name=name value='anthropic-3'", page, "the name is typed in already, ready to change")

    def stand_in(self, preset, stub):
        """Point a preset's address at a stub for this test (the real one is a provider or a fixed local port)."""
        real = routerlm.PRESETS[preset]
        routerlm.PRESETS[preset] = real[:3] + (stub.url,)
        self.addCleanup(routerlm.PRESETS.__setitem__, preset, real)

    def test_a_key_lists_its_models_and_one_click_puts_one_first(self):
        self.stand_in("anthropic", self.claude)
        self.claude.listed = ["claude-opus-5-5", "claude-opus-5-5-20260101", "claude-opus-4-5-20251101"]
        self.start()
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-test"})
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-wrong"})
        page = self.settings()
        self.assertNotIn("claude-opus-5-5-20260101", page, "a dated copy goes when its plain name is listed")
        self.assertIn("claude-opus-4-5-20251101", page, "a dated id with no plain name stays")
        self.assertIn("<option value='claude-opus-5-5' selected>", page, "the provider's models, beside the key")
        self.assertIn("Anthropic rejected this API key", page, "a bad key shows up before anything depends on it")
        r = self.post("/settings", {"do": "key_use", "name": "anthropic", "model": "claude-opus-5-5"})
        self.assertEqual(r.status, 303)
        answer = json.loads(self.file())["lineups"]["answer"]["models"]
        self.assertEqual(answer, ["anthropic-claude-opus-5-5", "cloud-claude", "cloud-openai", "local-qwen27"])
        self.assertEqual(self.ask().target, "anthropic-claude-opus-5-5")

    def test_quick_start_with_a_cloud_model_first(self):
        self.stand_in("anthropic", self.claude)
        self.stand_in("lmstudio", self.local)
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp}
        self.start()
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-test"})
        self.assertIn("Cloud model as Primary", self.settings())
        r = self.post("/settings", {"do": "quick_start", "pick": f"{self.local.url}|qwen3.6-27b-q4_k_m",
                                    "cloud": "anthropic|claude-opus-5-5"})
        self.assertEqual(json.loads(self.file())["lineups"]["answer"]["models"],
                         ["anthropic-claude-opus-5-5", "lmstudio-qwen3.6-27b-q4_k_m"])
        self.assertEqual(self.ask(key=None).target, "anthropic-claude-opus-5-5")

    def test_use_it_on_a_key_before_any_role_sets_everything_up(self):
        self.stand_in("anthropic", self.claude)
        self.stand_in("lmstudio", self.local)
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp}
        self.start()
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-test"})
        r = self.post("/settings", {"do": "key_use", "name": "anthropic", "model": "claude-opus-5-5"})
        self.assertEqual(r.status, 303, "no app key to show: apps on this Mac need none")
        self.assertEqual(json.loads(self.file())["lineups"]["answer"]["models"],
                         ["anthropic-claude-opus-5-5", "lmstudio-qwen3.6-27b-q4_k_m"])

    def test_change_a_roles_model_with_one_pick(self):
        self.stand_in("anthropic", self.claude)
        self.stand_in("lmstudio", self.local)
        self.local.listed = ["qwen3.6-27b-q4_k_m", "gemma-4-31b"]
        self.start()
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-test"})
        page = self.settings()
        self.assertIn("value='new:anthropic|claude-opus-5-5'", page, "a key's model, not set up yet")
        self.assertIn(f"value='new-local:{self.local.url}|gemma-4-31b'", page, "an engine's model, not set up yet")
        r = self.post("/settings", {"do": "role_save", "name": "answer", "t0": "new:anthropic|claude-opus-5-5",
                                    "t1": "local-qwen27", "timeout_first_token_s": "1"})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.ask().target, "anthropic-claude-opus-5-5")
        r = self.post("/settings", {"do": "role_save", "name": "summarize",
                                    "t0": f"new-local:{self.local.url}|gemma-4-31b", "timeout_first_token_s": "1"})
        self.assertEqual(r.status, 303)
        self.assertEqual(json.loads(self.file())["lineups"]["summarize"]["models"], ["lmstudio-gemma-4-31b"])
        time.sleep(0.3)  # one health pass for the new local model
        self.assertEqual(self.ask("summarize").target, "lmstudio-gemma-4-31b")

    def test_try_it(self):
        self.start()
        page = self.page().raw.decode()
        self.assertIn("<h2>Try it</h2>", page)
        self.assertIn("setInterval", page, "refreshes itself, but not while someone types")
        r = self.post("/try", {"role": "answer", "question": "What did Q-1042 quote?"})
        self.assertEqual(r.status, 200)
        self.assertIn("Answered by <b>claude-opus-5-5 (cloud)</b>", r.raw.decode())
        self.assertIn("hello from claude-opus-5-5", r.raw.decode())
        self.assertIn("What did Q-1042 quote?", r.raw.decode(), "the question stays in the box")
        before = len(self.claude.requests)
        r = self.post("/try", {"role": "answer", "question": "Private?", "local_only": "1"})
        self.assertIn("Answered by <b>qwen3.6-27b-q4_k_m (local)</b>", r.raw.decode())
        self.assertEqual(len(self.claude.requests), before, "local-only never reached the cloud")
        self.assertIn("type a question", self.post("/try", {"role": "answer", "question": " "}).raw.decode())
        self.assertEqual([x["app"] for x in self.log_lines(at_least=2)][-2:], ["try-it", "try-it"])
        self.assertEqual(self.post("/try", {"role": "answer", "question": "x"}, origin="http://evil.example").status,
                         403)
        del self.srv.router.cfg.admin_password
        self.srv.router.cfg.admin_password = None
        self.as_other_computer()
        self.assertEqual(self.post("/try", {"role": "answer", "question": "x"}, auth="").status, 403)

    def test_engines_with_a_start_button(self):
        real = routerlm.PRESETS["lmstudio"]
        routerlm.PRESETS["lmstudio"] = real[:3] + (closed_port(),)  # installed, server off
        self.addCleanup(routerlm.PRESETS.__setitem__, "lmstudio", real)
        started = []
        real_starter = routerlm.engine_starter
        routerlm.engine_starter = lambda p: [sys.executable, "-c", "pass"] if p == "lmstudio" else None
        self.addCleanup(setattr, routerlm, "engine_starter", real_starter)
        self.start()
        page = self.settings()
        self.assertIn("Start LM Studio", page)
        self.assertIn("not found on this machine", page, "llama-server: nothing to start")
        r = self.post("/settings", {"do": "engine_start", "name": "lmstudio"})
        self.assertEqual(r.status, 303)
        self.assertIn("LM+Studio+started", r.headers.get("Location", ""))

    def test_lm_studio_models_say_which_are_loaded(self):
        self.stand_in("lmstudio", self.local)
        self.local.listed = ["qwen3.6-27b-q4_k_m", "gemma-4-31b"]
        self.local.loaded = ["gemma-4-31b"]
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp}
        self.start()
        page = self.settings()
        self.assertIn("gemma-4-31b (LM Studio on this machine, loaded)", page)
        self.assertIn("qwen3.6-27b-q4_k_m (LM Studio on this machine)<", page)
        self.assertIn("running, 2 models, 1 loaded", page)

    def test_a_cloud_only_lineup_works(self):
        for preset in ("lmstudio", "ollama", "llama-server"):  # no engine on this machine
            real = routerlm.PRESETS[preset]
            routerlm.PRESETS[preset] = real[:3] + (closed_port(),)
            self.addCleanup(routerlm.PRESETS.__setitem__, preset, real)
        self.stand_in("anthropic", self.claude)
        self.raw = {"listen": "127.0.0.1:0", "log_dir": self.tmp}
        self.start()
        self.post("/settings", {"do": "key_add", "name": "", "value": "sk-ant-test"})
        r = self.post("/settings", {"do": "key_use", "name": "anthropic", "model": "claude-opus-5-5"})
        self.assertEqual(r.status, 303)
        self.assertEqual(json.loads(self.file())["lineups"]["answer"]["models"], ["anthropic-claude-opus-5-5"])
        self.assertEqual(self.ask(key=None).target, "anthropic-claude-opus-5-5")
        self.assertNotIn("must be local", self.settings(), "no warning either")

    def test_keys_are_kept_apart_and_never_shown(self):
        self.start()
        self.assertEqual(self.post("/settings", {"do": "key_add", "name": "anthropic-2",
                                                 "value": "sk-ant-SECRET-9876"}).status, 303)
        self.assertEqual(json.loads(self.file("secrets.json")), {"keys": {"anthropic-2": "sk-ant-SECRET-9876"}})
        self.assertEqual(os.stat(os.path.join(self.tmp, "secrets.json")).st_mode & 0o777, 0o600)
        self.assertNotIn("SECRET", self.file())
        page = self.settings()
        self.assertIn("…9876", page)
        self.assertNotIn("SECRET", page)
        r = self.post("/settings", {"do": "key_add", "name": "anthropic-2", "value": "x"})
        self.assertIn("already an API key named anthropic-2", r.raw.decode())
        self.post("/settings", {"do": "key_replace", "name": "anthropic-2", "value": "sk-ant-NEW-1111"})
        self.assertIn("NEW-1111", self.file("secrets.json"))
        self.assertEqual(self.post("/settings", {"do": "key_delete", "name": "anthropic-2"}).status, 303)
        self.assertEqual(json.loads(self.file("secrets.json")), {"keys": {}})

    def test_add_a_model_and_route_to_it_live(self):
        self.start()
        self.post("/settings", {"do": "key_add", "name": "claude-2", "value": "sk-ant-test"})
        add = {"do": "target_add", "preset": "anthropic", "base_url": self.claude.url, "model": "claude-opus-5-5",
               "key": "secret:claude-2"}
        r = self.post("/settings", add)
        self.assertEqual(r.status, 303)
        self.assertNotIn("Short name", self.settings(), "RouterLM makes the short name; the screens don't show it")
        tid = "anthropic-claude-opus-5-5"
        self.assertEqual(self.post("/settings", {"do": "role_save", "name": "answer", "t0": tid,
                                                 "t1": "cloud-openai", "t2": "local-qwen27", "lane": "interactive",
                                                 "data": "cloud-ok", "timeout_first_token_s": "1"}).status, 303)
        r = self.ask()
        self.assertEqual((r.status, r.target), (200, tid), "no restart needed")
        self.assertEqual(self.claude.requests[-1][1].get("x-api-key"), "sk-ant-test", "the key from Settings")
        t = next(t for t in json.loads(self.file())["models"] if t["id"] == tid)
        self.assertEqual(t["api_key"], "secret:claude-2")
        self.assertIn("claude-opus-5-5 (cloud) is already set up", self.post("/settings", add).raw.decode())
        self.post("/settings", {"do": "key_add", "name": "claude-3", "value": "sk-ant-test"})
        self.assertEqual(self.post("/settings", dict(add, key="secret:claude-3")).status, 303, "a second API key")
        self.assertIn(f"{tid}-2", [x["id"] for x in json.loads(self.file())["models"]])
        r = self.post("/settings", {"do": "key_delete", "name": "claude-2"})
        self.assertIn("used by claude-opus-5-5 (cloud)", r.raw.decode())

    def test_a_bad_change_is_refused_whole(self):
        self.start()
        before = self.file()
        r = self.post("/settings", {"do": "role_save", "name": "answer", "t0": ""})
        self.assertEqual(r.status, 400)
        self.assertIn("models must list at least one model", r.raw.decode())
        self.assertEqual(self.file(), before)
        self.assertEqual(self.ask().target, "cloud-claude")
        r = self.post("/settings", {"do": "target_delete", "name": "local-qwen27"})
        self.assertIn("is in answer, summarize, reason", r.raw.decode())
        r = self.post("/settings", {"do": "raw", "raw": "{not json"})
        self.assertEqual(r.status, 400)
        self.assertIn("{not json", r.raw.decode(), "the text is kept for fixing")
        self.assertEqual(self.file(), before)

    def test_callers_get_a_key_shown_once(self):
        self.start()
        r = self.post("/settings", {"do": "caller_add", "name": "nightly", "data": "local-only"})
        key = re.search(r"<div class=key>(rlm_[^<]+)</div>", r.raw.decode()).group(1)
        self.assertNotIn(key, self.file())
        KEYS["nightly"] = key
        self.addCleanup(KEYS.pop, "nightly")
        r = self.ask(key="nightly")
        self.assertEqual((r.status, r.target), (200, "local-qwen27"), "local-only ceiling")
        r = self.post("/settings", {"do": "caller_key", "name": "nightly"})
        self.as_other_computer()  # on this Mac no key is needed at all
        self.assertEqual(self.ask(key="nightly").status, 401, "the old key stops working")
        KEYS["nightly"] = re.search(r"<div class=key>(rlm_[^<]+)</div>", r.raw.decode()).group(1)
        self.assertEqual(self.ask(key="nightly").status, 200)
        self.post("/settings", {"do": "caller_delete", "name": "nightly"})
        self.assertEqual(self.ask(key="nightly").status, 401)

    def test_export_and_import(self):
        """Rene 10/15: "add Export and Import buttons to Settings; exports leave the API keys out"."""
        self.start()
        self.post("/settings", {"do": "key_add", "name": "anthropic", "value": "sk-ant-SECRET-77"})
        page = self.settings()
        self.assertIn("href=/settings/export", page)
        self.assertIn("type=file id=import", page)
        r = self.raw_request("GET", "/settings/export", {})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Content-Disposition"), 'attachment; filename="routerlm.json"')
        self.assertEqual(r.raw.decode(), self.file())
        self.assertNotIn("SECRET", r.raw.decode(), "the API keys stay out")
        exported = json.loads(r.raw)
        exported["models"].append({"id": "cloud-elsewhere", "location": "cloud", "adapter": "openai",
                                   "base_url": self.openai.url, "model": "gpt-5.5", "api_key": "secret:elsewhere"})
        r = self.post("/settings", {"do": "raw", "raw": json.dumps(exported), "imported": "1"})
        self.assertEqual(r.status, 303)
        self.assertIn("Settings+imported", r.headers.get("Location", ""))
        self.assertIn(("cloud-elsewhere", "API key missing"), self.alerts(), "its key isn't on this machine")
        bad = dict(exported, lineups={"answer": {"models": ["nowhere"]}})
        r = self.post("/settings", {"do": "raw", "raw": json.dumps(bad), "imported": "1"})
        self.assertIn("Not saved.", r.raw.decode(), "an import goes through the same checks")
        self.as_other_computer()
        self.assertEqual(self.raw_request("GET", "/settings/export", {}).status, 401, "other computers: the password")

    def test_password_for_other_computers(self):
        del self.raw["admin_password"]
        self.start()
        self.assertIn("Off: no password is set", self.settings())
        r = self.post("/settings", {"do": "password", "password": "new-password-1", "repeat": "new-password-2"})
        self.assertIn("don&#x27;t match", r.raw.decode())
        self.assertEqual(self.post("/settings", {"do": "password", "password": "new-password-1",
                                                 "repeat": "new-password-1"}).status, 303)
        self.assertIn("pbkdf2_sha256", self.file())
        self.assertNotIn("new-password-1", self.file())
        self.as_other_computer()
        self.assertEqual(self.page().status, 401)
        self.assertEqual(self.page(self.basic("new-password-1")).status, 200)

    def test_raw_edit_keeps_the_password(self):
        self.start()
        raw = json.loads(self.file())
        del raw["admin_password"]
        raw["cooldown_s"] = 5
        self.assertEqual(self.post("/settings", {"do": "raw", "raw": json.dumps(raw)}).status, 303)
        saved = json.loads(self.file())
        self.assertEqual((saved["cooldown_s"], "admin_password" in saved), (5, True))
        self.assertEqual(self.page(self.basic()).status, 200)

    def test_unchanged_targets_keep_their_health(self):
        self.claude.mode = "500"
        self.start()
        self.assertEqual(self.ask().target, "cloud-openai")
        before = self.srv.router.cfg.targets["cloud-openai"]
        self.post("/settings", {"do": "key_add", "name": "spare", "value": "sk-spare"})
        self.assertTrue(self.srv.router.cfg.targets["cloud-claude"].is_down(), "still in its cooldown")
        self.assertIs(self.srv.router.cfg.targets["cloud-openai"], before)
        t = next(t for t in json.loads(self.file())["models"] if t["id"] == "cloud-openai")
        self.post("/settings", dict({k: t.get(k, "") for k in ("model", "base_url", "location", "adapter",
                                                               "api_key")}, do="target_save", name="cloud-openai",
                                    max_parallel="3"))
        self.assertIsNot(self.srv.router.cfg.targets["cloud-openai"], before, "a changed target is rebuilt")

    def test_new_listen_address_asks_for_a_restart(self):
        called = []
        real = routerlm.restart
        routerlm.restart = lambda: called.append(True)
        self.addCleanup(setattr, routerlm, "restart", real)
        self.start()
        r = self.post("/settings", {"do": "general", "listen": "127.0.0.1:18099", "cooldown_s": "60"})
        self.assertEqual(r.status, 303)
        self.assertIn("Restart now", self.settings())
        self.assertIn("Back in a few seconds", self.post("/settings", {"do": "restart"}).raw.decode())
        time.sleep(0.8)
        self.assertEqual(called, [True])

    def test_forms_only_from_the_page_and_the_password(self):
        self.start()
        self.assertEqual(self.post("/settings", {"do": "key_add", "name": "x", "value": "y"},
                                   origin="http://evil.example").status, 403)
        self.as_other_computer()
        self.assertEqual(self.post("/settings", {"do": "key_add", "name": "x", "value": "y"},
                                   auth=f"Bearer {KEYS['capsule']}").status, 401)
        self.assertEqual(self.run_button("local-qwen27", auth=f"Bearer {KEYS['capsule']}").status, 401)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "secrets.json")))

    def test_changes_logged_without_secrets(self):
        self.start()
        self.post("/settings", {"do": "key_add", "name": "openai-2", "value": "sk-proj-SECRET-55"})
        log = "".join(self.file(n) for n in os.listdir(self.tmp) if n.startswith("routerlm-"))
        self.assertIn('"change":"API key added: openai-2"', log)
        self.assertNotIn("SECRET", log)

    def test_models_the_engines_list(self):
        self.start()
        page = self.settings()
        self.assertIn("<option value='qwen3.6-27b-q4_k_m'>", page)
        self.assertIn(f"<td>{self.local.url}</td><td>running, 1 model</td>", page)


if __name__ == "__main__":
    unittest.main()
