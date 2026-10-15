# stubs.py v0.4 — 2026-10-15
# Changelog:
# - v0.4 (2026-10-15): loaded: LM Studio's GET /api/v0/models, which says which models are in memory.
# - v0.3 (2026-10-15): GET /models takes a query string and, on a stub with a key, refuses a wrong one (401), as the
#   providers' model listings do.
# - v0.2 (2026-10-14): reply_fn (an answer per request, for the check set and its grader); props (llama-server's
#   GET /props, for the weights pin).
# - v0.1 (2026-10-14): Fake engines and providers for the spec's fault tests (ARCHITECTURE.md section 18).
"""Fake model engines and providers for RouterLM's tests.

A Stub records every request it receives and does what its mode says with the next one. Modes:
  ok         answer (Anthropic flavor: a thinking block first, as current Claude models send)
  500 / 429 / 401 / 400   fail with that status, in the provider's own error shape
  no_credit  the provider's out-of-credit answer (Anthropic: 400 "credit balance is too low";
             OpenAI: 429 insufficient_quota)
  refuse     the model declines the content
  break      the stream starts, then the connection drops mid-answer
  tool       answer with a tool call
  slow_first headers (and Anthropic's message_start) arrive, then nothing
  hang       read the request, never answer
reply_fn, when set, makes the reply from the request body. props, when set, is served at GET /props, the way
llama-server reports the model file it loaded.
BlackHole accepts connections and never answers (a firewall that drops). closed_port() refuses them.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANTHROPIC_NO_CREDIT = ("Your credit balance is too low to access the Anthropic API. "
                       "Please go to Plans & Billing to upgrade or purchase credits.")
OPENAI_NO_CREDIT = "You exceeded your current quota, please check your plan and billing details."


class Stub:
    def __init__(self, flavor="openai", model="stub-model", key=None):
        self.flavor, self.model, self.key = flavor, model, key
        self.mode, self.reply, self.listed = "ok", f"hello from {model}", None
        self.reply_fn, self.props, self.loaded = None, None, None
        self.hold_s = 0.0   # pause after the first token, to keep a slot busy
        self.hang_s = 30.0
        self.requests = []  # (path, headers, body) for every POST
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class BlackHole:
    """Completes the TCP handshake (the kernel does that from the backlog) and never says a word."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/v1"

    def close(self):
        self.sock.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def handler_for(stub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, obj):
            raw = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def start_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

        def event(self, data, name=None):
            head = f"event: {name}\n".encode() if name else b""
            self.wfile.write(head + b"data: " + json.dumps(data).encode() + b"\n\n")
            self.wfile.flush()

        def do_GET(self):
            path = self.path.split("?")[0]
            given = self.headers.get("x-api-key") or (self.headers.get("Authorization") or "")[7:]
            if path == "/api/v0/models" and stub.loaded is not None:
                self.send_json(200, {"object": "list", "data": [
                    {"id": m, "state": "loaded" if m in stub.loaded else "not-loaded"}
                    for m in (stub.listed or [stub.model])]})
            elif path.endswith("/models") and stub.key and given != stub.key:
                self.send_json(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            elif path.endswith("/models"):
                self.send_json(200, {"object": "list", "data": [{"id": m} for m in (stub.listed or [stub.model])]})
            elif path == "/props" and stub.props is not None:
                self.send_json(200, stub.props)
            else:
                self.send_json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            with stub.lock:
                stub.requests.append((self.path, dict(self.headers), body))
            mode = stub.mode
            if mode == "hang":
                time.sleep(stub.hang_s)
                return
            if self.path.endswith("/embeddings"):
                self.send_json(200, {"object": "list", "model": stub.model,
                                     "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                                     "usage": {"prompt_tokens": 3, "total_tokens": 3}})
            elif stub.flavor == "anthropic":
                self.anthropic(mode, body)
            else:
                self.openai(mode, body)

        # -- OpenAI-compatible -------------------------------------------------------------------------

        def openai(self, mode, body):
            errors = {
                "500": (500, {"message": "The server had an error", "type": "server_error", "code": None}),
                "429": (429, {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}),
                "401": (401, {"message": "Incorrect API key provided", "type": "invalid_request_error",
                              "code": "invalid_api_key"}),
                "400": (400, {"message": "Invalid value for 'messages'", "type": "invalid_request_error",
                              "code": None}),
                "no_credit": (429, {"message": OPENAI_NO_CREDIT, "type": "insufficient_quota",
                                    "code": "insufficient_quota"}),
            }
            if stub.key and self.headers.get("Authorization") != f"Bearer {stub.key}":
                mode = "401"
            if mode in errors:
                status, err = errors[mode]
                return self.send_json(status, {"error": err})
            words = (["I can't help with that."] if mode == "refuse" else
                     [w + " " for w in self.reply(body).split()])
            finish = {"refuse": "content_filter", "tool": "tool_calls"}.get(mode, "stop")

            def chunk(delta, fin=None):
                return {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": stub.model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}

            if not body.get("stream"):
                msg = {"role": "assistant", "content": "".join(words)}
                return self.send_json(200, {"id": "chatcmpl-stub", "object": "chat.completion", "model": stub.model,
                                            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                            "usage": {"prompt_tokens": 10, "completion_tokens": len(words)}})
            self.start_sse()
            self.event(chunk({"role": "assistant", "content": ""}))
            if mode == "slow_first":
                time.sleep(stub.hang_s)
                return
            if mode == "tool":
                self.event(chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                  "function": {"name": "lookup", "arguments": ""}}]}))
                for part in ('{"order"', ': "Q-1042"}'):
                    self.event(chunk({"tool_calls": [{"index": 0, "function": {"arguments": part}}]}))
            for i, w in enumerate(words):
                self.event(chunk({"content": w}))
                if i == 0 and stub.hold_s:
                    time.sleep(stub.hold_s)
                if mode == "break":
                    return  # the connection drops mid-answer
            self.event(chunk({}, finish))
            if (body.get("stream_options") or {}).get("include_usage"):
                self.event({"id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": stub.model,
                            "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": len(words),
                                                     "total_tokens": 10 + len(words)}})
            self.wfile.write(b"data: [DONE]\n\n")

        # -- Anthropic Messages ------------------------------------------------------------------------

        def reply(self, body):
            return stub.reply_fn(body) if stub.reply_fn else stub.reply

        def anthropic(self, mode, body):
            errors = {
                "500": (529, "overloaded_error", "Overloaded"),
                "429": (429, "rate_limit_error", "Number of request tokens has exceeded your rate limit"),
                "401": (401, "authentication_error", "invalid x-api-key"),
                "400": (400, "invalid_request_error", "messages: at least one message is required"),
                "no_credit": (400, "invalid_request_error", ANTHROPIC_NO_CREDIT),
            }
            if stub.key and self.headers.get("x-api-key") != stub.key:
                mode = "401"
            if mode in errors:
                status, kind, message = errors[mode]
                return self.send_json(status, {"type": "error", "error": {"type": kind, "message": message}})
            self.start_sse()
            self.event({"type": "message_start", "message": {
                "id": "msg_stub", "type": "message", "role": "assistant", "model": stub.model, "content": [],
                "stop_reason": None, "usage": {"input_tokens": 12, "output_tokens": 1}}}, "message_start")
            self.event({"type": "ping"}, "ping")
            if mode == "slow_first":
                time.sleep(stub.hang_s)
                return
            # Current Claude models think first; with display omitted the block carries no text.
            self.event({"type": "content_block_start", "index": 0,
                        "content_block": {"type": "thinking", "thinking": ""}}, "content_block_start")
            self.event({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "signature_delta", "signature": "sig"}}, "content_block_delta")
            self.event({"type": "content_block_stop", "index": 0}, "content_block_stop")
            stop = {"refuse": "refusal", "tool": "tool_use"}.get(mode, "end_turn")
            if mode == "tool":
                self.event({"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}}, "content_block_start")
                for part in ('{"order"', ': "Q-1042"}'):
                    self.event({"type": "content_block_delta", "index": 1,
                                "delta": {"type": "input_json_delta", "partial_json": part}}, "content_block_delta")
                self.event({"type": "content_block_stop", "index": 1}, "content_block_stop")
            else:
                text = ["I can't help with that."] if mode == "refuse" else [w + " " for w in self.reply(body).split()]
                self.event({"type": "content_block_start", "index": 1,
                            "content_block": {"type": "text", "text": ""}}, "content_block_start")
                for i, w in enumerate(text):
                    self.event({"type": "content_block_delta", "index": 1,
                                "delta": {"type": "text_delta", "text": w}}, "content_block_delta")
                    if i == 0 and stub.hold_s:
                        time.sleep(stub.hold_s)
                    if mode == "break":
                        return
                self.event({"type": "content_block_stop", "index": 1}, "content_block_stop")
            self.event({"type": "message_delta", "delta": {"stop_reason": stop},
                        "usage": {"output_tokens": 5}}, "message_delta")
            self.event({"type": "message_stop"}, "message_stop")

    return Handler
