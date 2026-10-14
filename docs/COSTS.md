<!--
COSTS.md v0.3 — 2026-10-14
Changelog:
- v0.3 (2026-10-14): Second-provider note: RouterLM fails over and alerts on an account out of credit (spec v0.4).
- v0.2 (2026-10-14): Rewritten around what it costs to keep working (Rene: "routerlm is not about money. its about staying working"). Payback, break-even and batch comparisons dropped.
- v0.1 (2026-10-14): First draft.
-->

# What it costs to keep working

RouterLM buys one thing: work that keeps going through outages, rate limits, price and terms changes, and models a provider retires. This page lists what that costs, so you can set it against what a stopped day costs you.

**Prices are as of 2026-10-14**: US list prices for hardware, and the providers' published per-token rates.

## The box

The local model at the end of every list needs a box to run on ([HARDWARE.md](HARDWARE.md)). Spread over three years, with power at $0.15 per kWh:

| Box | Price | Per month over 36 months, with power |
|---|---|---|
| Small: Mac mini M6, 32 GB | $1,499 | about $43 |
| **Office (reference): Mac mini M5 Pro, 64 GB** | **$2,999** | **about $85** |
| Large: Mac Studio M5 Max, 128 GB | $5,399 | about $153 |
| Large, PC: Ryzen AI Max+ 395, 128 GB | about $3,600 | about $103 |

The box costs the same whether the internet is up or down, and whether it carries one role or all of them.

## Running it

- **Power.** With cloud models answering first, the box idles most of the day. At a 15 W average that's about 130 kWh a year, around $20.
- **Care, counted in hours.** Setup takes days the first time: choosing the local model, writing the check set ([ARCHITECTURE.md, section 13](ARCHITECTURE.md#13-the-check-set)), configuring the box. After that, plan on an hour or two a month: OS and engine updates, a check-set run after any model change, and a read through the log for failovers and slow answers. Someone has to own this. A box nobody looks after falls behind like any other server.
- **Replacement.** Hardware breaks. Plan for a day without the box, or keep a second one set up and current.

## What you keep paying providers

With the default order, cloud models answer first, so the provider bill stays about what it is today. For a reference office of 10 people asking 15 questions each per working day (about 3,150 questions a month, at roughly 6,000 tokens in and 600 out per question):

| Model tier | Examples | Per million tokens, in / out | Per month |
|---|---|---|---|
| Premium | Claude Fable 5.1, GPT-6 Astra | $10 / $50 | $284 |
| Large | Claude Opus 5.5 | $4 / $20 | $113 |
| Mid | Claude Sonnet 5.5, GPT-6.1 Sol | $2 / $10 | $57 |
| Small | Claude Haiku 5.5, GPT-6 Luna | $0.10 / $0.50 | $3 |

Models that think before answering bill that reasoning as output tokens, which can double or triple the output on hard questions. Prompt caching and batch discounts lower the figures. Your own log is the real number once anything is running.

Three things change around the edges:

- **A second provider account.** It sits second on the list and mostly sees traffic when the first provider is down, so its bill stays small. It still needs a current key and a card that works. An empty account on either provider makes RouterLM move to the next model and raise an alert, but a lapsed backup is still best found before the day it's needed.
- **The local floor costs nothing per request.** During an outage the work moves to the box, and the provider meters stop with it.
- **Roles that run locally first** add nothing to the provider bill. In the reference configuration that's summaries and tags.

## What a stopped day costs you

Only you know this number. A rough version:

```
cost_of_a_stop = people_who_stop × hours_stopped × cost_per_hour
               + work that misses a deadline because of it
```

The Office box comes to about $1,020 a year, power included. Put your number next to it.

## Your numbers

```
questions_per_month = people × questions_per_person_per_day × working_days
provider_per_month  = questions_per_month × (input_tokens × input_price
                                            + output_tokens × output_price) ÷ 1,000,000
box_per_month       = box_price ÷ months_of_life
                      + average_watts × 730 ÷ 1,000 × price_per_kWh
```

Prices are per million tokens; a month is about 730 hours. Once RouterLM is running, take `input_tokens` and `output_tokens` from its log rather than from estimates.

## Sources

Retrieved 2026-10-14.

- Claude prices: [anthropic.com/pricing](https://www.anthropic.com/pricing)
- OpenAI prices: [developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing)
- Hardware prices: see [HARDWARE.md, Sources](HARDWARE.md#sources)
