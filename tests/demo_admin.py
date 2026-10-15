# demo_admin.py v0.3 — 2026-10-15
# Changelog (latest two; older entries: `git log -- RouterLM/impl/tests/demo_admin.py`):
# - v0.3 (2026-10-15): The new names (Rene): test questions in test-questions/questions.json, the config's models.
# - v0.2 (2026-10-15): No login on this machine.
"""RouterLM on 127.0.0.1:18080 in front of fake local AI apps and cloud companies, with some traffic, an API key
alert and two test-questions runs.

    python3 tests/demo_admin.py

Then open http://127.0.0.1:18080/. Ctrl-C stops it.
Nothing leaves the machine; everything lives in a temporary folder.
"""
import http.client
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), HERE]
import routerlm  # noqa: E402
import test_spec as ts  # noqa: E402
from stubs import Stub  # noqa: E402

PORT = 18080


def main():
    os.environ["RLM_TEST_ANTHROPIC"] = "sk-ant-test"
    os.environ["RLM_TEST_OPENAI"] = "sk-openai-test"
    tmp = tempfile.mkdtemp(prefix="routerlm-demo-")
    claude = Stub("anthropic", "claude-opus-5-5", key="sk-ant-test")
    openai = Stub("openai", "gpt-5.5", key="sk-openai-test")
    local = Stub("openai", "qwen3.6-27b-q4_k_m")
    embedder = Stub("openai", "nomic-embed-text-v2")
    raw = ts.sketch(claude.url, openai.url, local.url, embedder.url, tmp)
    raw["listen"] = f"127.0.0.1:{PORT}"
    raw["health_interval_s"] = 2
    model = os.path.join(tmp, "qwen3.6-27b-q4_k_m.gguf")
    with open(model, "wb") as f:
        f.write(b"GGUF demo weights")
    local.props = {"model_path": model}
    next(t for t in raw["models"] if t["id"] == "local-qwen27")["weights_sha256"] = routerlm.hash_file(model)
    os.makedirs(os.path.join(tmp, "test-questions"))
    with open(os.path.join(tmp, "test-questions", "questions.json"), "w") as f:
        json.dump(ts.check_set(), f)
    answers = dict(ts.ANSWERS)
    local.reply_fn = openai.reply_fn = ts.answer_by_question(answers)

    srv = routerlm.start(routerlm.load_config(raw, tmp))
    time.sleep(0.5)

    def ask(role, key="capsule"):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        c.request("POST", "/v1/chat/completions", json.dumps({"model": role, "messages": [
            {"role": "user", "content": "What did Q-1042 quote for the stage lights?"}]}),
            {"Authorization": f"Bearer {ts.KEYS[key]}"})
        c.getresponse().read()
        c.close()

    for _ in range(3):
        ask("answer")
    ask("summarize", key="hr-reports")
    ask("answer", key="hr-reports")
    claude.mode = "401"  # a rejected key: fails over to OpenAI and raises an alert
    ask("reason")
    claude.mode = "ok"
    claude.reply_fn = ts.stub_grader  # from here on the Claude stub grades the answers
    cfg = srv.router.cfg
    routerlm.run_check_set(cfg, routerlm.load_check_set(cfg), cfg.targets["local-qwen27"], srv.router.log)
    answers["delivery included"] = "No, the customer picks up."
    routerlm.run_check_set(cfg, routerlm.load_check_set(cfg), cfg.targets["cloud-openai"], srv.router.log)
    print(f"Open http://127.0.0.1:{PORT}/  (Ctrl-C stops)", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
