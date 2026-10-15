<!--
APPLIANCE.md v0.8 — 2026-10-15
Changelog (latest two; older entries: `git log -- RouterLM/publish/repo/docs/APPLIANCE.md`):
- v0.8 (2026-10-15): Admin page: a dot per model in each lineup (Rene: "yes").
- v0.7 (2026-10-15): One name per thing (Rene): lineups, apps, local AI app, test questions. A lineup ends where its owner decides (Rene): the acceptance test expects every lineup with a local model to answer offline, not every lineup.
-->

# The appliance

One box on a shelf in the office. It runs the local models and RouterLM, and Capsule Base beside them if the business uses it. The business owns it outright. When the internet or an AI provider is down, this box keeps answering. Requests marked `local-only` are answered here and nowhere else.

This document describes how to set one up so it runs unattended, comes back by itself after the ordinary things that go wrong, and can be looked after by anyone who reads these documents.

## What runs on it

```
+--------------------------------------------------------------------+
|  Office-tier box, e.g. Mac mini M5 Pro, 64 GB                      |
|                                                                    |
|   Capsule Base (optional)      RouterLM              admin page    |
|   records + vectors  ------->  lineups · failover --> status light |
|   Postgres + pgvector          local-only · log                    |
|                                  |              \                  |
|                    local AI app                to cloud providers  |
|                    (llama-server):             (#1 in the lineup   |
|                    answer model ·              for hard work)      |
|                    embeddings                                      |
|                                                                    |
|   firewall: inbound from the LAN only · outbound to providers only |
+--------------------------------------------------------------------+
        | wired Ethernet                              | UPS
     office LAN                                    wall power
```

Memory on a 64 GB box, as an example:

| Part | Memory |
|---|---|
| OS and services | 8 GB |
| Answer model, 27–35B at Q4 | 17–21 GB |
| Context for two or three conversations at once | 6–12 GB |
| Embedding model | 1 GB |
| Postgres + pgvector (Capsule Base) | 4–8 GB |
| Headroom | 10 GB or more |

## Software

- **OS:** macOS on a Mac (Apple Silicon or Intel); Debian or Ubuntu LTS on a PC. Windows isn't supported as the box. Windows machines call RouterLM over the network like any other app.
- **Local AI app:** llama.cpp's `llama-server`, one instance per loaded model, each on its own local port.
- **RouterLM:** one service ([ARCHITECTURE.md](ARCHITECTURE.md)).
- **Capsule Base:** its own services, as its spec describes.
- **Service manager:** launchd on macOS, systemd on Linux. Everything starts at boot and restarts if it exits.
- **Git on the box:** holds RouterLM's config, the test questions and this machine's notes, so every change has a date and a reason.

No container platform, no orchestrator, no vendor agent. Anyone with a terminal can stop, read and restart every piece.

## Network

- **Wired Ethernet**, a fixed address on the LAN, and a name people can type, such as `ai.office.lan`.
- **Inbound: LAN only.** No port forwarding and no remote-access service open to the internet. If someone looks after the box remotely, they come in over a VPN the owner runs and can switch off.
- **Outbound: blocked** by the box's own firewall (pf on macOS, nftables on Linux), except for an allow-list.
  - The list holds the cloud providers in RouterLM's config and time sync (NTP). A box with no cloud providers has nothing else on it.
  - An update host goes on the list while someone is updating, and comes off afterwards.
  - The OS itself tries to reach its vendor for updates and services. The allow-list decides whether it can.
- **The proof:** unplug the office internet. Every lineup with a local model still answers from it, with the light on yellow. That is the acceptance test for the whole idea.

## Power and restarts

- **A small UPS** rides out short outages and shuts the box down cleanly in long ones.
- **Start after power returns.** Set the box to power on by itself, and every service to start at boot without anyone logging in.
- **Disk encryption is a trade-off.** With FileVault (macOS) or LUKS (Linux) on, a stolen box gives up nothing. After a power loss, though, the disk waits for a person to unlock it, and nothing answers until they do. With encryption off, the box comes back by itself, and a stolen box gives up everything on it.
  - If Capsule Base's records live on the box, turn encryption on and name the person who unlocks it after an outage.
  - Planned restarts can skip the unlock once (`fdesetup authrestart` on macOS).

## Updates

Nothing on the box updates itself.

- **OS and local AI app:** on a schedule the owner sets (monthly is enough). Open the firewall for the update host, update, close it, run the test questions.
- **Models:** a new model gets its own entry. Download it, pin its hash, run the test questions ([ARCHITECTURE.md, section 13](ARCHITECTURE.md#13-test-questions)), then put it in the old model's place in the lineup. The old model stays on disk until the new one has a week of clean logs.
- **RouterLM config:** edit it, commit it with a reason, restart the service.

## Admin page

One page. On the box itself it opens with no password; other computers on the LAN use the password the owner sets in Settings:

- each lineup in order (#1, the Primary, first), its status light, and when it last answered, with a dot per model: green when its last try answered, red when it's down or its last try failed, grey when it hasn't been asked;
- which model answered the most recent requests, and whether it was local or cloud;
- requests and tokens per day, per app;
- the last test-questions run for each model: date, model, and pass / safe-miss / unsafe-miss counts;
- disk free, memory in use, uptime;
- alerts: a rejected cloud API key, a provider account out of credit, a model file whose hash changed, debug capture switched on.

## Backup and replacement

- **RouterLM holds no business data.** Back up its config, the test questions and the logs. They're small; a nightly copy to another machine is enough.
- **Model weights don't need a backup.** They can be downloaded again, and the pinned hashes prove the new copy is the same file.
- **Capsule Base's database is the business data.** Back it up as the Capsule Base spec describes. It's the one thing on the box that can't be downloaded again.
- **If the box dies:** set up a new box, restore the config and the test questions, download the pinned weights, restore Capsule Base's data, run the test questions, and put it back in service. Downloads take most of the time. An office that can't go a day without it keeps a second box set up and current.

## Acceptance test

Before the box goes into service:

1. **Unplug the office internet.** Every lineup with a local model answers from it, the lights turn yellow, and nothing hangs. Plug it back in; the lights return to green after the cooldown.
2. **Test `local-only`.** Stop every local AI app and send a `local-only` request. It fails with a clear error, and the firewall log shows nothing leaving.
3. **Stop the local AI app.** Lineups with only local models (summaries, embeddings) turn red within seconds and their apps get a clear error, not a hang. Lineups with a cloud model first stay green. Start it again; everything returns to green.
4. **Pull the power, then restore it.** The box comes back (or waits for the unlock, if encrypted), and every service starts without anyone logging in to start it.
5. **Run the test questions** against every model in every lineup. Every gate passes.
6. **Run a full working day of real use**, then read the log: response times, failovers, errors.

## Care

A box like this needs someone. Monthly: apply updates, re-run the test questions, read the log for slow answers and failovers, check disk space and the UPS battery. When the business's records change shape, the test questions change with them. A better model, when one ships, goes through the same gate as the first one did.

## Who owns what

The business owns the box, the config, the test questions, the API keys and the logs. Nothing on it reports to anyone outside. Any competent builder can take over its care from these documents. For one built and set up for you, see [emmausdev.com](https://emmausdev.com).
