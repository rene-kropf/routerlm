<!--
HARDWARE.md v0.3 — 2026-10-14
Changelog:
- v0.3 (2026-10-14): Box test ends with a working day on the local model alone, cloud targets off.
- v0.2 (2026-10-14): Intro states what the box is for: carrying the work alone when the providers can't.
- v0.1 (2026-10-14): First draft. Prices and model names as of 2026-10-14.
-->

# Hardware

How to size the box for the local model, and what that costs today. That model is the one still answering when the internet or every provider is down, so size the box for it to carry a working day alone.

**Prices and model names in this file are as of 2026-10-14, US list prices.** Both change within months. The sizing method in the first half holds up longer than the tables in the second.

## Sizing in three numbers

1. **Memory decides what fits.** The model's weights, its working memory for each conversation (the KV cache), and everything else on the box have to fit in RAM at the same time.
2. **Memory bandwidth decides how fast it writes.** Producing each token means reading the model's active weights once.
3. **Compute decides how fast it reads.** Before the first token appears, the engine processes the whole prompt. For questions that carry thousands of tokens of retrieved records, this is the wait people notice.

CPU core count, SSD speed and network speed barely matter. The SSD only changes how long a model takes to load.

### Memory: what fits

At 4-bit quantization (Q4, the usual choice for local use), weights take roughly 0.6 GB per billion parameters:

| Model size | Weights at Q4, approx. |
|---|---|
| 8B | 5 GB |
| 12B | 7 GB |
| 20B | 12–13 GB |
| 27B | 16–17 GB |
| 35B | 21 GB |
| 70B | 42 GB |
| 120B (mixture of experts) | 60–65 GB |

Add to that:

- **Context (KV cache).** It grows with context length and with the number of conversations served at once. Budget 2–8 GB per active 32K-token conversation for a mid-size model; it varies a lot by architecture. The engine prints the real figure when it loads a model.
- **An embedding model**: 1–2 GB at most.
- **Everything else**: the OS, the engine, RouterLM, and anything sharing the box, such as Capsule Base's Postgres. Allow 8–12 GB.

On a Mac, the GPU can use only part of unified memory by default, roughly two-thirds to three-quarters depending on the machine. The limit can be raised (`sysctl iogpu.wired_limit_mb`), at the expense of everything else on the box.

Rule of thumb: buy about twice the weights of the largest model you plan to run.

### Bandwidth: how fast it writes

Upper bound: **tokens per second ≈ memory bandwidth ÷ bytes read per token.** For a dense model, every token reads all the weights. A mixture-of-experts (MoE) model reads only the experts it activates for that token, which is why a 35B MoE model can write several times faster than a 27B dense one.

| Model at Q4 | Read per token | 170 GB/s | 307 GB/s | 614 GB/s |
|---|---|---|---|---|
| 27B dense | ~16 GB | ≤ 10 tok/s (tight fit in 32 GB) | ≤ 19 | ≤ 38 |
| 35B MoE, ~3B active | ~2 GB | doesn't fit in 32 GB | ≤ 150 | ≤ 300 |
| 120B MoE, ~5B active | ~3–4 GB | doesn't fit | doesn't fit in 64 GB | ≤ 150–200 |

These are ceilings. Real engines land at roughly half to three-quarters of them. Published tests of a 120B MoE model on 256–273 GB/s machines measured about 33–38 tokens per second with llama.cpp.

For reference, a person reads at roughly 5–8 tokens per second. Anything above 10 keeps ahead of the reader.

### Compute: how long until the first word

**Time to first token ≈ prompt tokens ÷ prompt-processing speed.** A question that carries 6,000 tokens of records waits 6 seconds at 1,000 tokens per second, and 20 seconds at 300.

Apple Silicon and AMD's unified-memory chips process prompts more slowly than NVIDIA hardware; published comparisons show NVIDIA several times faster on this step. Spec sheets rarely give this number. Measure it with your real prompt length before you buy.

## Tiers

As of 2026-10-14. Mac prices are Apple's configurator prices; the PC prices are street prices.

| Tier | Example machine | Memory | Bandwidth | Price (US) | Runs well |
|---|---|---|---|---|---|
| Small | Mac mini M6, 32 GB, 512 GB SSD | 32 GB | ~170 GB/s | $1,499 | Embeddings; 8–20B models for tagging, summaries, short answers |
| **Office (reference)** | **Mac mini M5 Pro, 64 GB, 1 TB SSD** | **64 GB** | **307 GB/s** | **$2,999** | **27–35B models for grounded answers, plus embeddings and a database on the same box** |
| Large | Mac Studio M5 Max (40-core GPU), 128 GB, 1 TB SSD | 128 GB | ~614 GB/s | $5,399 | 120B-class MoE or 70B dense; several models loaded at once |
| Large, PC | Ryzen AI Max+ 395 mini PC, 128 GB (several makers) | 128 GB | ~256 GB/s | $3,300–3,900 | The same models as Large, writing more slowly; runs Linux |
| Large, NVIDIA | NVIDIA DGX Spark (GB10), 128 GB | 128 GB | 273 GB/s | ~$4,700 | The same models; the fastest prompt processing in this group |
| Very large | Mac Studio M5 Ultra, 256 GB, 1 TB SSD | 256 GB | ~1.2 TB/s | $9,499 | 200B+ MoE models; rarely needed for office work |

**Why the Office tier is the reference.** 64 GB holds a 27–35B answer model, an embedding model, a database and context for two or three conversations at once, with room left over. At 307 GB/s, a 35B MoE model writes quickly and a 27B dense model still writes faster than people read. It's small and quiet. Apple rates the previous-generation (M4 Pro) Mac mini at 5 W idle and 140 W maximum, and an inference box spends most of the day idle. The 48 GB configuration saves $400 and leaves little headroom. The step to 128 GB nearly doubles the price; take it only if the check set shows the 30B-class model falls short on your questions.

**Memory prices rose sharply in 2026.** The 128 GB AMD mini PCs that sold for about $2,000 at their 2025 launch list for $3,300 and up as of October 2026. Check current prices before planning around this table.

### Other shapes

- **A desktop with a discrete GPU** (24–32 GB of video memory) is fast for any model that fits in video memory and stuck when one doesn't. It draws several hundred watts under load and makes noise. A good fit for a 27B-class model with fast prompt processing; a poor fit for 70B and up.
- **Linux boxes** run the same engines. llama.cpp supports CUDA, ROCm and Vulkan. Headless Linux suits an appliance well.
- **Multi-GPU servers** run the largest open models. That's beyond what an office needs.

## Models

As of 2026-10-14. These are examples: names and sizes change monthly, and sources disagree on details. Check each model card for its license and the memory figure for the exact quantization you'll run.

| Job | Examples | Notes |
|---|---|---|
| Embeddings | nomic-embed-text (768 dimensions), bge-m3, Qwen3-Embedding | Pick one and keep it ([ARCHITECTURE.md, section 9](ARCHITECTURE.md#9-embeddings)) |
| Small tier | gpt-oss-20b, Gemma 4 12B | OpenAI states gpt-oss-20b runs within 16 GB in its native 4-bit format |
| Office tier | Qwen3.6 27B (dense), Qwen3.6-35B-A3B (MoE), Gemma 4 31B | The MoE writes several times faster. Quality differences show up only on your own questions, so run the check set on both. |
| Large tier | gpt-oss-120b (MoE) | About 63 GB in its native 4-bit format; fits 128 GB with room for a second model |

## Engines

All of these serve the OpenAI-compatible API, so RouterLM works with any of them.

- **llama.cpp (`llama-server`)**: MIT-licensed, runs on Mac, Linux and Windows, headless, scriptable, small. The reference engine for the appliance.
- **LM Studio, Ollama**: friendlier on a desk and good for trying models.
- **MLX-based servers**: fast on Apple Silicon.
- **vLLM, SGLang**: built for NVIDIA servers with many users at once.

## Test the box you're considering

The numbers above narrow the choice. This test makes it:

1. Load the candidate answer model.
2. Send a real prompt: your system prompt, a typical set of retrieved records, and one question. Measure time to first token and tokens per second.
3. Send two at once, as two people would. Measure again.
4. Run the check set ([ARCHITECTURE.md, section 13](ARCHITECTURE.md#13-the-check-set)).
5. Switch the cloud targets off and run a full working day of real use on the local model alone, then read the log.

If the box gets through step 5 with the answer model you need, it's the right size. That day is the one it exists for.

## Power

An inference box sits idle most of the day. At a 15 W average, a year uses about 130 kWh, around $20 at $0.15 per kWh. Discrete-GPU machines draw much more under load.

## Sources

Retrieved 2026-10-14.

- Apple configurator prices for Mac mini (M6, M5 Pro) and Mac Studio (M5 Max, M5 Ultra), as tabulated at launch: [Daring Fireball, August 2026](https://daringfireball.net/2026/08/configurations_and_pricing_for_new_mac_minis_and_mac_studios)
- Mac mini M5 Pro memory bandwidth: [AppleInsider review](https://appleinsider.com/articles/26/10/05/mac-mini-m5-pro-review-mini-gets-serious-with-one-caveat); M6 bandwidth: [Travis Media](https://travis.media/blog/new-mac-mini-m6-m5-pro-specs-price-preorder/); Mac Studio overview: [MacRumors](https://www.macrumors.com/roundup/mac-studio/)
- Ryzen AI Max+ 395 mini PC prices and measured speeds: [ComputingForGeeks](https://computingforgeeks.com/ryzen-ai-max-395-mini-pc-comparison/), [Liliputing](https://liliputing.com/more-ryzen-ai-max-395-mini-pcs-with-128gb-are-now-available-if-you-can-afford-one/)
- DGX Spark price, bandwidth and measured speeds: [StarryHope](https://www.starryhope.com/ai/dgx-spark-local-ai-hardware-landscape/)
- Local model rankings: [BenchLM](https://benchlm.ai/best/local-llm), [Pinggy](https://pinggy.io/blog/top_5_local_llm_tools_and_models/)
- gpt-oss memory figures: [OpenAI](https://openai.com/index/introducing-gpt-oss/)
