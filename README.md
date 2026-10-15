<!--
README.md v0.10 — 2026-10-15
Changelog (latest two; older entries: `git log -- RouterLM/publish/repo/README.md`):
- v0.10 (2026-10-15): Status line follows routerlm.py 0.13 (Export and Import in Settings) and its 108 tests.
- v0.9 (2026-10-15): The code is here (Rene): routerlm.py, its tests and the guide; Run it; the Status line says what's built and tested.
-->

# RouterLM

A design for keeping AI work going when a model or a provider stops. RouterLM is a small service in front of every model an office uses. When a provider goes down, rate-limits you, changes its prices or terms, or retires the model you built on, the work moves to the next model in the lineup you set. Put a model that runs on a box in the building in that lineup, and work continues even with the internet down.

This repository holds the design, in [docs/](docs/), and a working implementation of it: `routerlm.py`, one Python file that needs nothing but Python itself. [Run it](#run-it) below starts it on a Mac.

## What it does

- **One endpoint on the office network.** Apps that use a model call RouterLM instead of a vendor. It speaks the API that local AI apps (llama.cpp, LM Studio, Ollama, vLLM) already speak, so pointing an app at it means changing one URL.
- **Lineups instead of model names.** Apps ask for `answer` or `summarize`. Each lineup lists its models in the order you choose; #1, the Primary, answers while it can.
- **Failover on plain triggers.** A model that's down, too slow, rate-limited or rejecting its API key hands off to the next in the lineup.
- **You decide where a lineup ends.** Cloud to cloud, cloud to local, local to cloud. With a local model in the lineup, work keeps going on the box in the building when the internet or every provider is down.
- **Keeping data in is a switch.** Mark an app or a job `local-only` and its requests never leave the building, even when a model fails.
- **Every answer labeled** with the model that wrote it, and a status light per lineup: green, yellow or red.
- **Test questions.** Your own questions with known answers, run against every model before it's trusted with real work.
- **It keeps nothing.** No chat history, no records. The apps that call it keep their own.

## Why

Work that depends on one AI provider stops when that provider stops. Outages, rate limits, price changes and retired models all arrive on the provider's schedule. RouterLM makes the model a preference, not a dependency. Every job has a lineup of models in the order you set, plain rules decide when to move to the next one, and a local model in the lineup keeps the work going when the cloud can't. Private records can stay on that box as well.

The longer argument for owning this layer, and the rest of the stack under it, is in the book *The Last Interface* ([last-interface.com](https://last-interface.com)).

## Who it's for

- Offices whose work stops when their AI provider does.
- People who already run models locally and want them to behave like infrastructure: one endpoint, written rules, a log, and a light that says when something's wrong.
- Small businesses, and the people who build for them, with some records that can't go to a third party.
- Anyone running [Capsule Base](https://github.com/rene-kropf/capsule-base).

If a day without your AI provider costs you nothing, you don't need the extra part.

## With Capsule Base

[Capsule Base](https://github.com/rene-kropf/capsule-base) turns a business database into something you can ask questions of: your records, on your hardware, with answers that cite them. Writing each answer is one call to a model over plain HTTP, and adding a provider there is one function. Send that call to RouterLM and Capsule keeps answering when a provider doesn't. Mark it `local-only` and Capsule's questions never leave the building: the records, the search index and the model that writes the answer all sit on the same shelf.

The two are separate on purpose. Capsule Base doesn't require RouterLM, and RouterLM works with any app that calls a model.

## Run it

You need a Mac with Python 3.9 or later, and at least one of these: a local AI app running a model (LM Studio, Ollama or llama-server), or an API key from Anthropic or OpenAI.

1. On this page, press the green "Code" button, then "Download ZIP". Move the `routerlm-main` folder it unzips to into your home folder.
2. Open Terminal and start RouterLM:

   ```bash
   cd routerlm-main && python3 routerlm.py serve
   ```

3. In a browser on the same Mac, open http://127.0.0.1:8090/. It opens Settings, with no password on the Mac itself.
4. Under Quick start, pick a model and press "Use it". With only an API key, add it under API keys and press "Use it" in its row.
5. In your app's settings, put `http://127.0.0.1:8090/v1` as the address (base URL) and `answer` as the model. On the same Mac the app needs no API key: put its name in the API key box, or leave it blank.

[docs/GUIDE.md](docs/GUIDE.md) walks through each step and every screen, for people who'd rather not live in a terminal. To run the tests: `python3 -m unittest discover -s tests`.

## Read in this order

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it works. Lineups, failover, local models, keeping data in, embeddings, test questions, failure paths.
2. [docs/HARDWARE.md](docs/HARDWARE.md): sizing a box by memory, bandwidth and prompt processing, with tiers and current prices.
3. [docs/COSTS.md](docs/COSTS.md): what it costs to keep working. The box, power, care, and the provider bill you keep paying.
4. [docs/APPLIANCE.md](docs/APPLIANCE.md): the box on the shelf. What runs on it, network, power, updates, the acceptance test, and care.

## Status

October 2026. `routerlm.py` 0.13 implements this design and is set up from a browser. Tested: 108 automated tests against fake models, covering the failure table (section 17 of the architecture), the `local-only` rule and the setup pages; and run on a Mac against real Anthropic and OpenAI models and local models in Ollama and LM Studio. Not built yet: the appliance as a whole ([docs/APPLIANCE.md](docs/APPLIANCE.md)), meaning the dedicated box, its firewall and the internet-off acceptance test.

Prices and model names carry the date they were checked. Expect them to be out of date within months.

## Want it built?

Builds are custom to each business: its records, its hardware, its rules about what may leave the building. See [emmausdev.com](https://emmausdev.com).

## License

MIT. See [LICENSE](LICENSE).

🐾 [Adopt a Pet — Get a Dog](https://www.petfinder.com/)
