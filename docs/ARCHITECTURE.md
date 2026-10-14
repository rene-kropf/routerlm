<!--
ARCHITECTURE.md v0.4 — 2026-10-14
Changelog:
- v0.4 (2026-10-14): An account out of credit counts as unavailable: fail over and alert, same as a rejected key (Rene).
- v0.3 (2026-10-14): Failure table: the local-engine crash row follows the new order.
- v0.2 (2026-10-14): Default order (Rene approved): lists run best-first, cloud usually first; every list ends with a local model; local-only is an opt-in, not the default; embeddings stay local.
- v0.1 (2026-10-14): First draft.
-->

# RouterLM architecture

RouterLM is one small service that sits between the programs that use a language model and the models themselves. Every model call in the building goes through it. It decides which model answers and moves to the next one when a model stops answering. A local model ends every list, so work continues when the internet or every provider is down. Requests marked private never leave the building. Every response records which model wrote it.

It stores no business data. The programs that call it own their records and their context.

**Contents**

1. [Where it sits](#1-where-it-sits)
2. [Principles](#2-principles)
3. [The front door](#3-the-front-door)
4. [Roles](#4-roles)
5. [The local floor, and keeping data in](#5-the-local-floor-and-keeping-data-in)
6. [Targets and adapters](#6-targets-and-adapters)
7. [Failover](#7-failover)
8. [What RouterLM does not do](#8-what-routerlm-does-not-do)
9. [Embeddings](#9-embeddings)
10. [Streaming](#10-streaming)
11. [Queues and priority](#11-queues-and-priority)
12. [Labels and the status light](#12-labels-and-the-status-light)
13. [The check set](#13-the-check-set)
14. [Logs](#14-logs)
15. [Configuration](#15-configuration)
16. [Security](#16-security)
17. [Failure paths](#17-failure-paths)
18. [Build notes](#18-build-notes)
19. [Scope](#19-scope)

## 1. Where it sits

```
  Capsule Base          scripts and         coding agent       anything else that
  (answers over         nightly jobs        on a desk          speaks the same API
   your records)        (summaries, tags)
        \                    |                   |                   /
         +-------------------+-------------------+------------------+
                                      |
                       RouterLM  (one endpoint on the LAN)
                       roles · failover · local floor · labels · log
                                      |
         +-----------------+----------+---------+--------------------+
         |                 |                    |                    |
   cloud provider A   cloud provider B    local engine         local engine
   (first, for hard   (second)            answer model:        embedding model
    work)                                 last on every list   (search stays local)
   ---------- outside the building ----------   ------- on the box -------
```

Callers don't know which model answers. They name a role, such as `answer` or `summarize`, and RouterLM maps the role to a model from a list a person wrote down.

## 2. Principles

1. **The model is a preference, not a dependency.** Swapping a model is a config change and a test run. Callers don't change.
2. **A local model is always on the list.** Every role ends with a model on hardware in the building. When the internet or every provider is down, work keeps going on that model. Requests marked `local-only` never leave the building at all.
3. **Stateless.** RouterLM holds no conversation history and no project knowledge. Callers send the full context with every request, so switching models partway through a conversation loses nothing. Delete RouterLM and you lose a routing convenience, nothing more.
4. **People set the routes.** Which model answers is decided per role, in a config file. No model chooses which model to use.
5. **Fewest moving parts.** One process, one config file, one log file, no database. If you run one model for one program, you don't need RouterLM: point the program straight at it. One model is one dependency, and RouterLM earns its place once there are two targets or more.
6. **Every answer is labeled.** The caller and the log always know which model produced a response.
7. **Formats you own.** Config is JSON, logs are JSON lines, and the API is the one local engines already speak.

## 3. The front door

RouterLM serves the OpenAI-compatible HTTP API on the local network:

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Chat requests, streamed or not |
| `POST /v1/embeddings` | Embeddings |
| `GET /v1/models` | The roles (and concrete models, for callers allowed to name them) |
| `GET /status` | Health per role and per target, as JSON |
| `GET /` | Admin page: status light, recent activity, check-set results |

Why this API: llama.cpp's server, LM Studio, Ollama, vLLM and the MLX servers all speak it, and so do most tools that call models. A program that can talk to any of them can talk to RouterLM by changing one base URL. The format was defined by one vendor and is now implemented by many open-source servers. That's a dependency worth naming, and it's still the most portable choice there is.

A caller names a role in the `model` field:

```json
{ "model": "answer", "messages": [ ... ] }
```

A caller may name a concrete model (`"model": "qwen3.6-27b-q4_k_m"`) if its key allows it. Most callers should use roles, so models can change underneath them.

Two optional request headers:

| Header | Values | Default |
|---|---|---|
| `X-RouterLM-Data` | `cloud-ok`, `local-only` | `cloud-ok` |
| `X-RouterLM-Lane` | `interactive`, `batch` | the role's lane |

Each caller has its own key: a bearer token issued by RouterLM, unrelated to any provider key. The key carries a ceiling. A key capped at `local-only` can't send `cloud-ok`, whatever the header says.

## 4. Roles

A role is a named job with an ordered list of targets. Four or five roles cover most offices:

| Role | Job | Typical target |
|---|---|---|
| `answer` | Grounded answers over records the caller retrieved | A cloud model, a second provider, then a 27–35B local model |
| `summarize` | Summaries, tags, classification, extraction; bulk work | The local model alone; batch work waits if it's down |
| `reason` | Hard, multi-step questions | The strongest cloud model, a second provider, then the largest local model |
| `embed` | Vectors for search | One pinned local embedding model ([section 9](#9-embeddings)) |

Each role sets:

- `targets`: the ordered list, best for the job first. The first one that can answer, does. The last one must be local ([section 5](#5-the-local-floor-and-keeping-data-in)).
- `lane`: `interactive` or `batch` ([section 11](#11-queues-and-priority)).
- `timeout_first_token_s`: how long to wait for the first token before trying the next target.
- `max_context`: requests longer than this are refused with a clear error. RouterLM never truncates silently.
- `data`: the most permissive data class the role may use. A `local-only` role can't reach a cloud target even when a caller sends `cloud-ok`.

Keep roles coarse. A role per program usually means the roles are standing in for settings that belong in the program.

## 5. The local floor, and keeping data in

These are the two rules the rest of the design protects.

**Every list ends with a local model.** Each role's last target runs on hardware in the building. When the internet is down, or every provider on the list is down, rate-limited or rejecting its key, requests land on that model. Answers may be slower or plainer, and the work keeps going. RouterLM refuses to start if any role's last target isn't local.

The local model has to be good enough to carry a working day alone. The check set ([section 13](#13-the-check-set)) is how you find out, and [HARDWARE.md](HARDWARE.md) covers sizing the box for it.

**Keeping data in the building is a switch.** Every request has a data class, `cloud-ok` (the default) or `local-only`. The effective class is the strictest of three: the request header, the caller's key ceiling, and the role's `data` setting. Mark a caller or a role `local-only` and its requests never leave the building, including on failover. If no local target can answer one, it fails with a clear error. A failed answer can be retried in a minute. A private record sent to a third party can't be called back.

**Targets are marked by location.** Each target carries `"location": "local"` or `"location": "cloud"`. Local means hardware the owner controls, on the owner's network. RouterLM refuses to start if a `local` target points outside private address space, so a typo can't turn a cloud endpoint into a "local" one.

**The network backs it up.** The appliance's firewall lets outbound traffic reach only the providers in the config ([APPLIANCE.md](APPLIANCE.md)). A box with no cloud targets has an empty allow-list and no path out.

**What crosses for everything else.** When a `cloud-ok` request goes to a provider, the whole request goes: prompt, retrieved records, everything. RouterLM doesn't redact. Redaction that misses one field is worse than a clear rule, so the rule decides which requests may leave, before anything is sent.

Running locally keeps data in the building. On its own it doesn't make a system compliant with HIPAA, client-confidentiality rules or any other regulation. That takes its own review.

## 6. Targets and adapters

A target is one model on one engine:

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

- **`openai`** speaks the OpenAI-compatible API. It covers every local engine named above and any cloud provider with a compatible endpoint, OpenAI included. Adding such a provider is config, not code.
- **`anthropic`** translates to Anthropic's Messages API: system prompt placement, message shape, streaming events, usage fields.

Write a third adapter only when a provider worth having offers neither format.

`weights_sha256` pins a local model to an exact file. If the engine reports a different file (someone downloaded a "newer" quantization over it), the target is marked failed until the check set passes against the new file and the hash is updated.

## 7. Failover

Failover moves a request down its role's target list, skipping cloud targets for `local-only` requests. Because every list ends local, a request fails outright only when the local model is down too. Only conditions that mean "this target is unavailable" trigger a move:

| Condition | Fail over? | Also |
|---|---|---|
| Connection refused, network error | Yes | |
| No first token within the role's timeout | Yes | |
| HTTP 5xx | Yes | |
| HTTP 429 (rate limited) | Yes | |
| HTTP 401 / 403 (key rejected) | Yes | Alert: a key problem needs a person |
| Account out of credit, whatever the status code | Yes | Alert: a billing problem needs a person |
| Engine reports a different model or weights hash | Yes | Alert |
| HTTP 400 (bad request), other than out of credit | No | The request is wrong; the next model would reject it too |
| The model declined the content | No | Return the refusal. Shopping for a model that complies is not a policy. |
| Stream started, then broke | No | See [section 10](#10-streaming) |

**Out of credit.** Providers report an empty account differently. As of 2026-10, Anthropic answers with a 400 and a "credit balance is too low" message, and OpenAI with a 429 and the code `insufficient_quota`. Each adapter recognizes its own provider's version and reports it as out of credit, so the request moves on and a person hears about it. A rate limit clears by itself; an empty account doesn't.

**Health and cooldown.** A failed target is marked down for a cooldown (60 seconds is a reasonable start). During the cooldown the next target goes first. The first request after the cooldown tries the failed target again, and success marks it healthy. Local engines also get a cheap health check every few seconds. Cloud providers don't: nobody's key gets billed for background polling.

**The order is the policy.** RouterLM never re-sorts a list by speed or price. If the second target should go first, a person edits the config.

## 8. What RouterLM does not do

- **Model-picks-a-model routing.** A router that guesses which model a question deserves is hard to test, and its mistakes are invisible. Routing by role and rule is predictable, and the check set can prove it works. Rule-based routing may grow later ([section 19](#19-scope)).
- **Memory.** No chat history, no user profiles, no retrieval. Callers own their context.
- **Rewriting content.** RouterLM doesn't edit prompts, inject instructions or redact. It passes requests through and labels responses.
- **Training or fine-tuning.**
- **Billing.** It counts tokens for the log. It doesn't meter callers.

## 9. Embeddings

Vectors from different embedding models can't be compared. A search index built with one model is useless with another, so the `embed` role works differently from the rest:

- Exactly one target, and it's local, so search doesn't stop when a provider does. No failover to a different model.
- Every embedding response names the model. Callers should store that name beside each vector.
- If the embedding target is down, requests fail fast. Callers queue and retry; they don't fall back.
- Changing the embedding model is a planned job: stand the new model up as a second role (`embed-next`), re-embed everything, switch the caller over, retire the old role.

Embedding models are small, from a few hundred megabytes to a few gigabytes. Every tier in [HARDWARE.md](HARDWARE.md) runs one beside an answer model.

## 10. Streaming

Streamed responses pass through as server-sent events. Failover can happen only before the first token reaches the caller. Once tokens are flowing, a broken stream ends with an error event. RouterLM never finishes an answer with a different model: half an answer from one model glued to half from another is worse than an error the caller can retry.

## 11. Queues and priority

A local engine answers a few requests at a time (`max_parallel`). RouterLM keeps a queue per target with two lanes:

- **interactive**: someone is waiting. Always served first.
- **batch**: nightly summaries, re-indexing. Runs only when no interactive request is waiting.

Queues have a maximum depth. A request that can't start within its role's first-token timeout moves to the next target allowed by its data class, or fails with a 429 the caller can retry. A batch job never makes someone at a desk wait.

## 12. Labels and the status light

**Labels.** Every response says which target answered:

- The `model` field holds the concrete model, not the role name.
- `X-RouterLM-Target` and `X-RouterLM-Location` headers name the target and say whether it was local or cloud.

Callers should show this. People can judge an answer better when they can see what wrote it, and when behavior changes after a model update, the log shows which requests the new model touched.

**Status light.** One per role, on the admin page and in `GET /status`:

| Light | Meaning |
|---|---|
| Green | The role's first target is answering. |
| Yellow | Running on a later target (the local floor included), or answers are slow. |
| Red | No target in the role can answer. Requests fail fast with a clear error. |
| Grey | Shown by callers: RouterLM itself can't be reached. |

`GET /status` returns the same as JSON, so a caller can show the light in its own interface.

## 13. The check set

Failover proves a backup answers. It doesn't prove the backup answers correctly. The check set does that.

**What it is.** A file of 30–50 real questions from the business. Each one lists the facts a correct answer must contain and the things it must not claim. The owner writes it with someone who knows the records, from what people actually ask, awkward questions included. It contains business data, so it lives on the box, and it runs as `local-only` if the questions touch records that shouldn't leave.

**When it runs.** Before a target joins a role, after any model or engine update, and on demand from the admin page. A run pins one target; there's no failover inside a run.

**How it's graded.** Each answer lands in one of three bins:

- **Pass**: contains the required facts and claims nothing false.
- **Safe miss**: says it doesn't know, or asks a clarifying question. Costs a person a minute.
- **Unsafe miss**: states something false as fact. Costs trust, which is slow to earn back.

**The gate.** A role's first target: every question passes. Later targets, which answer only when the first is down: zero unsafe misses, and no more safe misses than the owner sets. A person grades, or a stronger model grades and a person reviews its grading the first time. If the questions touch private records, the grading model has to be local too.

Benchmarks measure someone else's questions. The check set is the only reliable answer to "is the local model good enough for our work?"

## 14. Logs

One JSON line per request, appended to a local file, one file per month:

```json
{"t":"2026-10-14T09:12:03Z","caller":"capsule","role":"answer","data":"cloud-ok",
 "target":"local-qwen27","location":"local","in":5840,"out":412,
 "ttft_ms":2100,"total_ms":9800,"outcome":"ok","failover_from":"cloud-openai"}
```

No prompt text and no response text: RouterLM holds no business data, and its log follows the same rule. A debug switch writes full requests to a separate file for troubleshooting. It's off by default, and while it's on, the admin page says so.

Nothing in the log is rotated out or deleted automatically. The owner decides what to keep.

## 15. Configuration

One JSON file, edited by a person and kept in git on the box, so every change has a date and a reason. A sketch:

```json
{
  "listen": "10.0.0.20:8080",
  "cooldown_s": 60,
  "callers": {
    "capsule":    { "key_sha256": "…", "ceiling": "cloud-ok" },
    "hr-reports": { "key_sha256": "…", "ceiling": "local-only" },
    "owner-desk": { "key_sha256": "…", "ceiling": "cloud-ok", "concrete_models": true }
  },
  "targets": [
    { "id": "cloud-claude",   "location": "cloud", "adapter": "anthropic",
      "key_ref": "keychain:routerlm-anthropic", "...": "..." },
    { "id": "cloud-openai",   "location": "cloud", "adapter": "openai",
      "key_ref": "keychain:routerlm-openai", "...": "..." },
    { "id": "local-qwen27",   "location": "local", "adapter": "openai", "...": "..." },
    { "id": "local-embed",    "location": "local", "adapter": "openai", "...": "..." }
  ],
  "roles": {
    "answer":    { "lane": "interactive", "data": "cloud-ok",
                   "timeout_first_token_s": 20, "max_context": 32768,
                   "targets": ["cloud-claude", "cloud-openai", "local-qwen27"] },
    "summarize": { "lane": "batch", "data": "cloud-ok",
                   "timeout_first_token_s": 60, "max_context": 32768,
                   "targets": ["local-qwen27"] },
    "reason":    { "lane": "interactive", "data": "cloud-ok",
                   "timeout_first_token_s": 30, "max_context": 65536,
                   "targets": ["cloud-claude", "cloud-openai", "local-qwen27"] },
    "embed":     { "lane": "batch", "data": "local-only",
                   "targets": ["local-embed"] }
  }
}
```

In this sketch `answer` tries Claude, then OpenAI, then the local 27B model. With the internet down, every role lands on `local-qwen27` or `local-embed`. Requests from `hr-reports` are capped at `local-only`, so they skip both cloud targets and go straight to the local model.

Provider keys never sit in this file. They live in the OS keychain, or in a secret file readable only by RouterLM's service account, and the config refers to them by name.

On start, RouterLM validates the file and refuses to run if:

- a role names a target that doesn't exist;
- a role's last target isn't local;
- a `local-only` role lists a cloud target;
- a `local` target's address is outside private address space;
- `embed` has more than one target, or its target isn't local.

A bad config should stop the service at start, while someone is watching, rather than surprise someone in the middle of a working day.

## 16. Security

- Listens on the LAN interface only. Never exposed to the internet, no port forwarding.
- Per-caller keys, stored as hashes. The admin page has its own key.
- Outbound traffic is blocked at the firewall except to configured cloud providers, if any, and to an update host while someone is updating ([APPLIANCE.md](APPLIANCE.md)).
- Runs as its own unprivileged service account.
- TLS on the LAN is optional. Turn it on when callers share a network with guests or devices you don't control.

## 17. Failure paths

| What breaks | What RouterLM does | What the caller sees |
|---|---|---|
| Local engine crashes | Marks the target down and tries the next target, if there is one; the service manager restarts the engine | Yellow, or red with a fast error if it was the last one the request could use |
| Engine hangs (no first token) | Timeout, then the next target | A slower answer; yellow |
| Every cloud provider down, or the office internet down | Every role lands on its local floor | Yellow; work continues |
| Every local target down, request `local-only` | Fails fast; nothing leaves | Red; "no local model available" |
| Every target down, request `cloud-ok` | Fails fast | Red |
| Cloud key rejected, or account out of credit | Next target; alert on the admin page | Yellow; key or billing alert |
| Model file replaced on disk | Hash mismatch; target refused until checked | Yellow or red; alert |
| Stream breaks mid-answer | Error event; no splice | An error the caller can retry |
| Queue full | Next target, or 429 | Retry later |
| Config invalid at start | Refuses to start | Grey |
| RouterLM down | Nothing answers | Grey |

## 18. Build notes

For whoever builds it:

- One process, a few hundred lines in most languages. Python's or Go's standard library is enough; no framework is needed.
- Keep the request path short: authenticate, resolve the data class, pick a target, forward, stream back, log. The status page and the check-set runner can be separate scripts that read the same config and log.
- Prove the failure table above with injected faults before trusting it: stop the engine, point a target at a port that accepts connections and never answers, return 500s and 429s from a stub, revoke a key, and return each provider's out-of-credit response from a stub. Each row should produce the listed light and the listed error.
- Prove the floor: block all outbound traffic and run every role. Each should answer from its local target, with the light on yellow.
- Prove `local-only` with a stub "cloud" target that records every request it receives. Run every `local-only` scenario, including all local targets down. The stub's record must stay empty.

## 19. Scope

**V1:** roles, failover with cooldown, the local floor, `local-only` requests, two adapters, a pinned local embedding role, two lanes, labels, the status light, logs, the check set, config validation.

**Later, if use shows the need:**

- Rule-based routing inside a role on request features a person can read: prompt length, caller, time of day. Still rules.
- More than one box behind one RouterLM, for example one for answers and one for batch work.
- A per-caller daily token budget for cloud targets.

**Out of scope:** model-based routing, conversation memory, redaction, fine-tuning.
