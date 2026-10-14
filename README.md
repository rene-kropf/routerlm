<!--
README.md v0.7 — 2026-10-14
Changelog:
- v0.7 (2026-10-14): Adopt a Pet line at the bottom, as in Capsule Base (Rene's every-page rule).
- v0.6 (2026-10-14): Code follows here once built and tested against the spec (Rene); was "none is planned".
- v0.5 (2026-10-14): COSTS line follows the rewrite; Capsule Base paragraph matches its own spec (adding a provider is one function).
- v0.4 (2026-10-14): Default order (Rene approved): best-first lists, a local model ends every list, local-only as an opt-in.
- v0.3 (2026-10-14): Opening and Why rewritten around staying working (Rene approved).
- v0.2 (2026-10-14): Status line narrowed: the switcher was prototyped, the failover tested.
- v0.1 (2026-10-14): First draft.
-->

# RouterLM

A design for keeping AI work going when a model or a provider stops. RouterLM is a small service in front of every model an office uses. When a provider goes down, rate-limits you, changes its prices or terms, or retires the model you built on, the work moves to the next model on the list. The last model on every list runs on a box in the building, so work continues even with the internet down.

For now this repository holds the design only. Code will be added here once it's built and tested against this spec. If you want to build your own sooner, everything you need is in [docs/](docs/).

## What it does

- **One endpoint on the office network.** Programs that use a model call RouterLM instead of a vendor. It speaks the API that local engines (llama.cpp, LM Studio, Ollama, vLLM) already speak, so pointing a program at it means changing one URL.
- **Roles instead of model names.** Programs ask for `answer` or `summarize`. A config file lists the models for each job, best first.
- **Failover on plain triggers.** A model that's down, too slow, rate-limited or rejecting its key hands off to the next on the list.
- **A local model ends every list.** When the internet or every provider is down, work keeps going on the box in the building.
- **Keeping data in is a switch.** Mark a program or a job `local-only` and its requests never leave the building, even when a model fails.
- **Every answer labeled** with the model that wrote it, and a status light per role: green, yellow or red.
- **A check set.** Your own questions with known answers, run against every model before it's trusted with real work.
- **It keeps nothing.** No chat history, no records. The programs that call it keep their own.

## Why

Work that depends on one AI provider stops when that provider stops. Outages, rate limits, price changes and retired models all arrive on the provider's schedule. RouterLM makes the model a preference, not a dependency. Every job has a list of models, plain rules decide when to move to the next one, and a local model is always on the list. Private records can stay on that box as well.

The longer argument for owning this layer, and the rest of the stack under it, is in the book *The Last Interface* ([last-interface.com](https://last-interface.com)).

## Who it's for

- Offices whose work stops when their AI provider does.
- People who already run models locally and want them to behave like infrastructure: one endpoint, written rules, a log, and a light that says when something's wrong.
- Small businesses, and the people who build for them, with some records that can't go to a third party.
- Anyone running [Capsule Base](https://github.com/rene-kropf/capsule-base).

If a day without your AI provider costs you nothing, you don't need the extra part.

## With Capsule Base

[Capsule Base](https://github.com/rene-kropf/capsule-base) turns a business database into something you can ask questions of: your records, on your hardware, with answers that cite them. Writing each answer is one call to a model over plain HTTP, and adding a provider there is one function. Send that call to RouterLM and Capsule keeps answering when a provider doesn't. Mark it `local-only` and Capsule's questions never leave the building: the records, the search index and the model that writes the answer all sit on the same shelf.

The two are separate on purpose. Capsule Base doesn't require RouterLM, and RouterLM works with any program that calls a model.

## Read in this order

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it works. Roles, failover, the local floor, keeping data in, embeddings, the check set, failure paths.
2. [docs/HARDWARE.md](docs/HARDWARE.md): sizing a box by memory, bandwidth and prompt processing, with tiers and current prices.
3. [docs/COSTS.md](docs/COSTS.md): what it costs to keep working. The box, power, care, and the provider bill you keep paying.
4. [docs/APPLIANCE.md](docs/APPLIANCE.md): the box on the shelf. What runs on it, network, power, updates, the acceptance test, and care.

## Status

Version 0.1, October 2026. This is a design document. Two parts have been prototyped: a manual switcher between local and cloud models, and deterministic failover between cloud providers, which was tested with injected faults. The appliance as a whole, as described here, has not been built. An implementation will be built against this spec and published here once it's tested.

Prices and model names carry the date they were checked. Expect them to be out of date within months.

## Want it built?

Builds are custom to each business: its records, its hardware, its rules about what may leave the building. See [emmausdev.com](https://emmausdev.com).

## License

MIT. See [LICENSE](LICENSE).

🐾 [Adopt a Pet — Get a Dog](https://www.petfinder.com/)
