<!--
ARCHITECTURE.md v0.13 — 2026-10-15
Changelog (latest two; older entries: `git log -- RouterLM/publish/repo/docs/ARCHITECTURE.md`):
- v0.13 (2026-10-15): Settings exports and imports the file (Rene).
- v0.12 (2026-10-15): Sections 15 and 16 match how it's set up now: from a browser, API keys in Settings, no password or key on the machine itself; the build note says what the implementation here is (Rene).
-->

# RouterLM architecture

RouterLM is one small service that sits between the apps that use a language model and the models themselves. Every model call in the building goes through it. Each app asks for a lineup, a list of models in the order the owner chose, and RouterLM moves to the next model when one stops answering. Put a local model in a lineup and work continues when the internet or every cloud company is down. Requests marked local-only never leave the building. Every response records which model wrote it.

It stores no business data. The apps that call it own their records and their context.

**Contents**

1. [Where it sits](#1-where-it-sits)
2. [Principles](#2-principles)
3. [The front door](#3-the-front-door)
4. [Lineups](#4-lineups)
5. [Local models, and keeping data in](#5-local-models-and-keeping-data-in)
6. [Models and adapters](#6-models-and-adapters)
7. [Failover](#7-failover)
8. [What RouterLM does not do](#8-what-routerlm-does-not-do)
9. [Embeddings](#9-embeddings)
10. [Streaming](#10-streaming)
11. [Queues and priority](#11-queues-and-priority)
12. [Labels and the status light](#12-labels-and-the-status-light)
13. [Test questions](#13-test-questions)
14. [Logs](#14-logs)
15. [Configuration](#15-configuration)
16. [Security](#16-security)
17. [Failure paths](#17-failure-paths)
18. [Build notes](#18-build-notes)
19. [Scope](#19-scope)

## 1. Where it sits

```
  Capsule Base          scripts and         coding agent       any other app that
  (answers over         nightly jobs        on a desk          speaks the same API
   your records)        (summaries, tags)
        \                    |                   |                   /
         +-------------------+-------------------+------------------+
                                      |
                       RouterLM  (one endpoint on the LAN)
                       lineups · failover · local-only · labels · log
                                      |
         +-----------------+----------+---------+--------------------+
         |                 |                    |                    |
   cloud company A    cloud company B     local AI app         local AI app
   (#1, the Primary,  (#2)                answer model (#3     embedding model
    for hard work)                        in this lineup)      (search stays local)
   ---------- outside the building ----------   ------- on the box -------
```

Apps don't know which model answers. They name a lineup, such as `answer` or `summarize`, and RouterLM hands the request to the first model in that lineup that can answer, in the order a person wrote down.

## 2. Principles

1. **The model is a preference, not a dependency.** Swapping a model is a config change and a test run. Apps don't change.
2. **The owner sets the order.** Each lineup runs in the order its owner wrote down and ends where the owner decides: cloud to cloud, cloud to local, local to cloud. A local model in a lineup keeps work going when the internet or every cloud company is down. Requests marked `local-only` never leave the building at all.
3. **Stateless.** RouterLM holds no conversation history and no project knowledge. Apps send the full context with every request, so switching models partway through a conversation loses nothing. Delete RouterLM and you lose a routing convenience, nothing more.
4. **People set the routes.** Which model answers is decided per lineup, in a config file. No model chooses which model to use.
5. **Fewest moving parts.** One process, one config file, one log file, no database. If you run one model for one app, you don't need RouterLM: point the app straight at it. One model is one dependency, and RouterLM earns its place once there are two models or more.
6. **Every answer is labeled.** The app and the log always know which model produced a response.
7. **Formats you own.** Config is JSON, logs are JSON lines, and the API is the one local AI apps already speak.

## 3. The front door

RouterLM serves the OpenAI-compatible HTTP API on the local network:

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Chat requests, streamed or not |
| `POST /v1/embeddings` | Embeddings |
| `GET /v1/models` | The lineups (and the models themselves, for apps allowed to name them) |
| `GET /status` | Health per lineup and per model, as JSON |
| `GET /` | Admin page: each lineup in order, status light, recent activity, test-question results |

Why this API: llama.cpp's server, LM Studio, Ollama, vLLM and the MLX servers all speak it, and so do most tools that call models. An app that can talk to any of them can talk to RouterLM by changing one base URL. The format was defined by one vendor and is now implemented by many open-source servers. That's a dependency worth naming, and it's still the most portable choice there is.

An app names a lineup in the `model` field:

```json
{ "model": "answer", "messages": [ ... ] }
```

An app may name a model directly (`"model": "qwen3.6-27b-q4_k_m"`) if its API key allows it. Most apps should use lineups, so models can change underneath them.

Two optional request headers:

| Header | Values | Default |
|---|---|---|
| `X-RouterLM-Data` | `cloud-ok`, `local-only` | `cloud-ok` |
| `X-RouterLM-Lane` | `interactive`, `batch` | the lineup's lane |

An app on the RouterLM machine needs no API key. Whatever it puts in its API key box is its name on the admin page; with nothing there, or something that looks like a key, it shows as "this Mac". If an app by that name is set up in RouterLM, its settings apply. A request from a web page on another site still needs a key. An app on another computer has its own API key: a bearer token issued by RouterLM, unrelated to the cloud companies' API keys. An app can be marked `local-only`; then everything it sends is `local-only`, whatever the header says.

## 4. Lineups

A lineup is a named list of models in order. #1, the Primary, answers while it can. When it can't, #2 does, and so on down. The owner decides the order and where the lineup ends; any order and any length is valid. Four or five lineups cover most offices:

| Lineup | Job | Typical models |
|---|---|---|
| `answer` | Grounded answers over records the app retrieved | A cloud model, a second company, then a 27–35B local model |
| `summarize` | Summaries, tags, classification, extraction; bulk work | The local model alone; batch work waits if it's down |
| `reason` | Hard, multi-step questions | The strongest cloud model, a second company, then the largest local model |
| `embed` | Vectors for search | One pinned local embedding model ([section 9](#9-embeddings)) |

Each lineup sets:

- `models`: the order, best for the job first. The first one that can answer, does.
- `lane`: `interactive` or `batch` ([section 11](#11-queues-and-priority)).
- `timeout_first_token_s`: how long to wait for the first token before trying the next model.
- `max_context`: requests longer than this are refused with a clear error. RouterLM never truncates silently.
- `data`: the most permissive data class the lineup may use. A `local-only` lineup can't reach a cloud model even when an app sends `cloud-ok`.

Keep lineups coarse. A lineup per app usually means the lineups are standing in for settings that belong in the app.

## 5. Local models, and keeping data in

**A local model keeps work going offline.** When the internet is down, or every cloud company in a lineup is down, rate-limited or rejecting its API key, requests land on the lineup's local model, if it has one. Answers may be slower or plainer, and the work keeps going. Whether a lineup has one, and where, is the owner's call.

A local model that carries the work alone has to be good enough for a working day. The test questions ([section 13](#13-test-questions)) are how you find out, and [HARDWARE.md](HARDWARE.md) covers sizing the box for it.

**Keeping data in the building is a switch.** Every request has a data class, `cloud-ok` (the default) or `local-only`. The effective class is the strictest of three: the request header, the app's `data` setting, and the lineup's `data` setting. Mark an app or a lineup `local-only` and its requests never leave the building, including on failover. If no local model can answer one, it fails with a clear error. A failed answer can be retried in a minute. A record sent to a third party can't be called back.

**Models are marked by location.** Each model carries `"location": "local"` or `"location": "cloud"`. Local means in the building: hardware the owner controls, on the owner's network. RouterLM refuses to start if a `local` model points outside private address space, so a typo can't turn a cloud endpoint into a "local" one.

**The network backs it up.** The appliance's firewall lets outbound traffic reach only the cloud companies in the config ([APPLIANCE.md](APPLIANCE.md)). A box with no cloud models has an empty allow-list and no path out.

**What crosses for everything else.** When a `cloud-ok` request goes to a cloud company, the whole request goes: prompt, retrieved records, everything. RouterLM doesn't redact. Redaction that misses one field is worse than a clear rule, so the rule decides which requests may leave, before anything is sent.

Running locally keeps data in the building. On its own it doesn't make a system private, secure or compliant with HIPAA, client-confidentiality rules or any other regulation. That takes its own review.

## 6. Models and adapters

A model entry is one model, in the cloud or on a local AI app:

```json
{
  "id": "local-qwen27",
  "location": "local",
  "adapter": "openai",
  "base_url": "http://127.0.0.1:8081/v1",
  "model": "qwen3.6-27b-q4_k_m",
  "weights_sha256": "9f2c…",
  "max_parallel": 2
}
```

Two adapters cover nearly everything:

- **`openai`** speaks the OpenAI-compatible API. It covers every local AI app named above and any cloud company with a compatible endpoint, OpenAI included. Adding such a company is config, not code.
- **`anthropic`** translates to Anthropic's Messages API: system prompt placement, message shape, streaming events, usage fields.

Write a third adapter only when a company worth having offers neither format.

`weights_sha256` pins a local model to an exact file. If the local AI app reports a different file (someone downloaded a "newer" quantization over it), the model is set aside, with an alert, and the next model in each of its lineups answers in its place. When nothing else in the lineup is left, it's tried anyway and stays set aside afterwards. It comes back when the test questions pass against the new file and the hash is updated, or when the old file is back.

## 7. Failover

Failover moves a request down its lineup, skipping cloud models for `local-only` requests. A request fails outright only when every model it may use can't answer. Only conditions that mean "this model is unavailable" trigger a move:

| Condition | Fail over? | Also |
|---|---|---|
| Connection refused, network error | Yes | |
| No first token within the lineup's timeout | Yes | |
| HTTP 5xx | Yes | |
| HTTP 429 (rate limited) | Yes | |
| HTTP 401 / 403 (API key rejected) | Yes | Alert: an API key problem needs a person |
| Account out of credit, whatever the status code | Yes | Alert: a billing problem needs a person |
| Local AI app reports a different model or weights hash | Yes | Alert |
| HTTP 400 (bad request), other than out of credit | No | The request is wrong; the next model would reject it too |
| The model declined the content | No | Return the refusal. Shopping for a model that complies is not a policy. |
| Stream started, then broke | No | See [section 10](#10-streaming) |

**Out of credit.** Cloud companies report an empty account differently. As of 2026-10, Anthropic answers with a 400 and a "credit balance is too low" message, and OpenAI with a 429 and the code `insufficient_quota`. Each adapter recognizes its own company's version and reports it as out of credit, so the request moves on and a person hears about it. A rate limit clears by itself; an empty account doesn't.

**Health and cooldown.** A failed model is set aside for a cooldown (60 seconds is a reasonable start). During the cooldown the next model goes first. The first request after the cooldown tries the failed model again, and success puts it back. When every model a request may use is sitting out its cooldown as the request comes in, RouterLM tries them anyway, in order, rather than answer that none can: an internet blip shouldn't cost a minute of answers. When a model that was up fails on the request, the ones in their cooldown aren't tried: give a model its 60 seconds before retrying it. The next model that's up still is. A model held out by a check is tried too when nothing else is left: its local AI app not answering or listing another model, or its file replaced or not checked yet. One with no API key isn't, since there's nothing to test. Local AI apps also get a cheap health check every few seconds. Cloud companies don't: nobody's API key gets billed for background polling.

**The order is the policy.** RouterLM never re-sorts a lineup by speed or price. If #2 should be the Primary, a person changes the order.

## 8. What RouterLM does not do

- **Model-picks-a-model routing.** A router that guesses which model a question deserves is hard to test, and its mistakes are invisible. Routing by lineup and rule is predictable, and the test questions can prove it works. Rule-based routing may grow later ([section 19](#19-scope)).
- **Memory.** No chat history, no user profiles, no retrieval. Apps own their context.
- **Rewriting content.** RouterLM doesn't edit prompts, inject instructions or redact. It passes requests through and labels responses.
- **Training or fine-tuning.**
- **Billing.** It counts tokens for the log. It doesn't meter apps.

## 9. Embeddings

Vectors from different embedding models can't be compared. A search index built with one model is useless with another, so the `embed` lineup works differently from the rest:

- Exactly one model, and it's local, so search doesn't stop when a cloud company does. No failover to a different model.
- Every embedding response names the model. Apps should store that name beside each vector.
- If the embedding model is down, requests fail fast. Apps queue and retry; they don't fall back.
- Changing the embedding model is a planned job: stand the new model up as a second lineup (`embed-next`), re-embed everything, switch the app over, retire the old lineup.

Embedding models are small, from a few hundred megabytes to a few gigabytes. Every tier in [HARDWARE.md](HARDWARE.md) runs one beside an answer model.

## 10. Streaming

Streamed responses pass through as server-sent events. Failover can happen only before the first token reaches the app. Once tokens are flowing, a broken stream ends with an error event. RouterLM never finishes an answer with a different model: half an answer from one model glued to half from another is worse than an error the app can retry.

## 11. Queues and priority

A local AI app answers a few requests at a time (`max_parallel`). RouterLM keeps a queue per model with two lanes:

- **interactive**: someone is waiting. Always served first.
- **batch**: nightly summaries, re-indexing. Runs only when no interactive request is waiting.

Queues have a maximum depth. A request that can't start within its lineup's first-token timeout moves to the next model allowed by its data class, or fails with a 429 the app can retry. A batch job never makes someone at a desk wait.

## 12. Labels and the status light

**Labels.** Every response says which model answered:

- The `model` field holds the model's own name, not the lineup name.
- `X-RouterLM-Model` and `X-RouterLM-Location` headers name the model entry and say whether it was local or cloud.

Apps should show this. People can judge an answer better when they can see what wrote it, and when behavior changes after a model update, the log shows which requests the new model touched.

**Status light.** One per lineup, on the admin page and in `GET /status`:

| Light | Meaning |
|---|---|
| Green | The lineup's Primary (#1) is answering. |
| Yellow | A later model is answering, or answers are slow. |
| Red | No model in the lineup can answer. Requests fail fast with a clear error. |
| Grey | Shown by apps: RouterLM itself can't be reached. |

**A dot per model.** Each model in a lineup also gets a dot, from what RouterLM already knows: green when its last try answered (or, for a local model not asked yet, when its local AI app is ready); red when it's down or its last try failed, and it stays red after the cooldown until the model answers again; grey when it hasn't been asked. Beside it: when it last answered, or when and why it last failed. Every failed try counts, not only the last one before an answer. A cloud backup that's never needed stays grey, since RouterLM doesn't send cloud companies questions in the background.

`GET /status` returns the same as JSON, so an app can show the light and the dots in its own interface.

## 13. Test questions

Failover proves a backup answers. It doesn't prove the backup answers correctly. The test questions do that.

**What they are.** A file of 30–50 real questions from the business. Each one lists the facts a correct answer must contain and the things it must not claim. The owner writes them with someone who knows the records, from what people actually ask, awkward questions included. They contain business data, so they live on the box, and they run as `local-only` if the questions touch records that shouldn't leave.

**When they run.** Before a model joins a lineup, after any model or local AI app update, and on demand from the admin page. A run asks one model; there's no failover inside a run.

**How they're graded.** Each answer lands in one of three bins:

- **Pass**: contains the required facts and claims nothing false.
- **Safe miss**: says it doesn't know, or asks a clarifying question. Costs a person a minute.
- **Unsafe miss**: states something false as fact. Costs trust, which is slow to earn back.

**The gate.** A lineup's Primary: every question passes. Later models, which answer only when the Primary can't: zero unsafe misses, and no more safe misses than the owner sets. A person grades, or a stronger model grades and a person reviews its grading the first time. If the questions touch private records, the grading model has to be local too.

Benchmarks measure someone else's questions. Your own test questions are the only reliable answer to "is the local model good enough for our work?"

## 14. Logs

One JSON line per request, appended to a local file, one file per month:

```json
{"t":"2026-10-14T09:12:03Z","app":"capsule","lineup":"answer","data":"cloud-ok",
 "model":"local-qwen27","location":"local","in":5840,"out":412,
 "ttft_ms":2100,"total_ms":9800,"outcome":"ok","failover_from":"cloud-openai"}
```

No prompt text and no response text: RouterLM holds no business data, and its log follows the same rule. A debug switch writes full requests to a separate file for troubleshooting. It's off by default, and while it's on, the admin page says so.

Nothing in the log is rotated out or deleted automatically. The owner decides what to keep.

## 15. Configuration

One JSON file, `routerlm.json`, beside `routerlm.py`. Settings, on the admin page, writes it; a person can also edit it as text, in Settings or in any editor. Settings' Export downloads it and Import loads one through the same checks; API keys never go with it. On a box, keep it in git, so every change has a date and a reason. An empty file is valid: the first start writes one with only the listen address, and the admin page opens to Settings. A sketch:

```json
{
  "listen": "10.0.0.20:8080",
  "cooldown_s": 60,
  "apps": {
    "capsule":    { "key_sha256": "…", "data": "cloud-ok" },
    "hr-reports": { "key_sha256": "…", "data": "local-only" },
    "owner-desk": { "key_sha256": "…", "data": "cloud-ok", "concrete_models": true }
  },
  "models": [
    { "id": "cloud-claude",   "location": "cloud", "adapter": "anthropic",
      "api_key": "keychain:routerlm-anthropic", "...": "..." },
    { "id": "cloud-openai",   "location": "cloud", "adapter": "openai",
      "api_key": "keychain:routerlm-openai", "...": "..." },
    { "id": "local-qwen27",   "location": "local", "adapter": "openai", "...": "..." },
    { "id": "local-embed",    "location": "local", "adapter": "openai", "...": "..." }
  ],
  "lineups": {
    "answer":    { "lane": "interactive", "data": "cloud-ok",
                   "timeout_first_token_s": 20, "max_context": 32768,
                   "models": ["cloud-claude", "cloud-openai", "local-qwen27"] },
    "summarize": { "lane": "batch", "data": "cloud-ok",
                   "timeout_first_token_s": 60, "max_context": 32768,
                   "models": ["local-qwen27"] },
    "reason":    { "lane": "interactive", "data": "cloud-ok",
                   "timeout_first_token_s": 30, "max_context": 65536,
                   "models": ["cloud-claude", "cloud-openai", "local-qwen27"] },
    "embed":     { "lane": "batch", "data": "local-only",
                   "models": ["local-embed"] }
  }
}
```

In this sketch `answer` tries Claude, then OpenAI, then the local 27B model. With the internet down, every lineup here lands on `local-qwen27` or `local-embed`. Requests from `hr-reports` are `local-only`, so they skip both cloud models and go straight to the local model.

Cloud API keys never sit in this file. Settings keeps them in `secrets.json` beside it, readable only by RouterLM's own user account, and afterwards shows only their last four characters; the config refers to each by name (`"api_key": "secret:anthropic"`). A key can also come from the macOS keychain, a file or an environment variable (`keychain:`, `file:`, `env:`). A model whose key is missing is set aside with an alert, so the key can be added from Settings.

On start, and on every save from Settings, RouterLM checks the file and refuses it, listing every problem at once, if:

- a lineup names a model that doesn't exist;
- a `local-only` lineup lists a cloud model;
- a `local` model's address is outside private address space;
- `embed` has more than one model, or its model isn't local;
- it would listen on every interface, or on an address outside private address space.

A bad config should stop the service at start, while someone is watching, rather than surprise someone in the middle of a working day. A save that fails the checks is refused whole, and nothing changes; one that passes takes effect at once, except a new listen address, which needs the Restart button.

## 16. Security

- Listens on 127.0.0.1, this machine only, until the owner gives it the box's LAN address in Settings. Never on every interface, never exposed to the internet, no port forwarding.
- Apps on the RouterLM machine need no API key; apps on other computers use their own, stored as hashes. A request from a web page on another site, or one that names another host, needs a key too. The admin page needs no password on the RouterLM machine itself. Other computers are shut out until the owner sets a password, stored as a salted hash. Settings takes forms only from its own page, so a web page in the owner's browser can't drive it.
- Outbound traffic is blocked at the firewall except to configured cloud companies, if any, and to an update host while someone is updating ([APPLIANCE.md](APPLIANCE.md)).
- On a box, runs as its own unprivileged service account; on a desk Mac, as the person who started it.
- TLS on the LAN is optional. Turn it on when apps share a network with guests or devices you don't control.

## 17. Failure paths

| What breaks | What RouterLM does | What the app sees |
|---|---|---|
| Local AI app crashes | Sets the model aside and tries the next model, if there is one; the service manager restarts the local AI app | Yellow, or red with a fast error if it was the last one the request could use |
| Local AI app hangs (no first token) | Timeout, then the next model | A slower answer; yellow |
| Every cloud company down, or the office internet down | Each lineup moves to its local model, if it has one | Yellow and work continues; red for a lineup with no local model |
| Every local model down, request `local-only` | Fails fast; nothing leaves | Red; "no local model available" |
| Every model down, request `cloud-ok` | Tries the ones sitting out a cooldown once more, then fails with a clear error | Red |
| Cloud API key rejected, or account out of credit | Next model; alert on the admin page | Yellow; API key or billing alert |
| Model file replaced on disk | Hash mismatch; the model is set aside and the next model in the lineup answers until it's checked; with nothing else left, it's tried anyway | Yellow, or red if it was the only one; alert |
| Stream breaks mid-answer | Error event; no splice | An error the app can retry |
| Queue full | Next model, or 429 | Retry later |
| Config invalid at start | Refuses to start | Grey |
| RouterLM down | Nothing answers | Grey |

## 18. Build notes

For whoever builds it:

- One process. Python's or Go's standard library is enough; no framework is needed. The implementation in this repository, `routerlm.py`, is one Python file of about 3,200 lines, with the admin page, Settings and the test-question runner in the same file.
- Keep the request path short: authenticate, resolve the data class, pick a model, forward, stream back, log. The status page and the test-questions runner can be separate scripts that read the same config and log.
- Prove the failure table above with injected faults before trusting it: stop the local AI app, point a model at a port that accepts connections and never answers, return 500s and 429s from a stub, revoke an API key, and return each company's out-of-credit response from a stub. Each row should produce the listed light and the listed error.
- Prove the offline path: block all outbound traffic and run every lineup that has a local model. Each should answer from it, with the light on yellow.
- Prove `local-only` with a stub "cloud" model that records every request it receives. Run every `local-only` scenario, including all local models down. The stub's record must stay empty.

## 19. Scope

**V1:** lineups, failover with cooldown, `local-only` requests, two adapters, a pinned local embedding lineup, two lanes, labels, the status light, logs, the test questions, config validation.

**Later, if use shows the need:**

- Rule-based routing inside a lineup on request features a person can read: prompt length, app, time of day. Still rules.
- More than one box behind one RouterLM, for example one for answers and one for batch work.
- A per-app daily token budget for cloud models.

**Out of scope:** model-based routing, conversation memory, redaction, fine-tuning.
