# Benchmarks

Hardware: Snapdragon X2 Elite CRD, 48 GB LPDDR5 (Adreno iGPU shares system RAM),
Windows 11 ARM64 + WSL2. Generation speed measured via `/v1/chat/completions`
(token usage ÷ wall time), single stream.

## llama.cpp on Adreno GPU (OpenCL backend) vs Ollama CPU baseline

Ollama ran CPU-only (its Adreno support lags llama.cpp). Same models, same
quants where possible. `pps` = prompt tok/s, `eps` = generation tok/s.

| Model | Quant | GPU eps (llama.cpp) | CPU eps (Ollama, ctx 8K) | Speedup |
|-------|-------|--------------------|--------------------------|---------|
| Qwen3-Coder-30B-A3B (MoE) | Q4_0 | **~31 tok/s** | 16.6 | ~1.9x |
| GPT-OSS-20B (MoE) | Q4_0 | ~26 tok/s | 7.9 | ~3.3x |
| Qwen3-8B (dense) | Q5_0 | ~22 tok/s | 21.0 | ~1.0x |
| Mistral-Nemo-12B (dense) | Q4_0 | ~17 tok/s | 13.5 | ~1.3x |
| Qwen2.5-Coder-1.5B (dense) | Q4_0 | ~90 tok/s | 90.7 | ~1.0x |

Reading the numbers honestly:

- **MoE models gain the most** on Adreno (3B-active 30B beats the dense 8B —
  31 vs 22 tok/s — while being far smarter for coding).
- Dense small models are **memory-bandwidth-bound**: the 8B runs at the same
  speed on CPU and GPU. The GPU win there is *headroom* (frees CPU cores),
  not raw speed.
- The 12B/20B sit in between (partial offload efficiency).

## Server tuning (2026-09-17, Snapdragon X2 Elite Extreme, driver 32.0.149.0)

`llama-bench -p 512 -n 128 -r 2 -ngl 99 -fa on` (raw log:
`logs/bench_review_2026-09-17.log`).

CPU threads with every layer on the GPU:

| Model | `-t 4` pp512 / tg128 | `-t 8` | `-t 18` (default) |
|-------|---------------------|--------|-------------------|
| Qwen3-Coder-30B-A3B Q4_0 | **525.5 / 35.6** | 525.7 / 35.2 | 501.8 / 34.6 |
| GPT-OSS-20B Q4_0 | 555.4 / **40.8** | – | 561.9 / 38.1 |
| Qwen3-8B Q5_0 | **265.7 / 18.5** | – | 207.7 ± 90 / 18.0 |

→ `-t 4` is as fast or faster, steadier, and leaves 14 cores idle (less power
and heat next to the NPU). Now the default in the panel and `serve_gpu.bat`.

KV cache quantization (`-ctk q8_0 -ctv q8_0`, 30B, `-t 8`): pp512 468.0,
tg128 **27.8** (−21 %). Not worth it on 48 GB: fp16 KV for the 30B at 64K is
only ~6.3 GB.

Speculative decoding, 30B served with `-c 16384 -t 4 -np 1`, temperature 0,
600 generated tokens. "gen" = write a new module; "edit" = reproduce ~8 KB of
existing code with a rename (what coding agents do all day):

| Mode | gen t/s | edit t/s | acceptance (edit) |
|------|--------:|---------:|-------------------|
| none (baseline) | 31.4 | 25.3 | – |
| `--spec-type ngram-mod` | 31.2 | **86.9** | 564/564 |
| draft Qwen3-0.6B Q8_0 on GPU | 12.0 | 13.1 | 531/537 |
| draft 0.6B on GPU + ngram-mod | 12.2 | 63.3 | 586/592 |
| draft 0.6B on CPU (`-devd none -td 4`) | 26.1 | 25.0 | – |

→ The draft model costs more than it saves: the Adreno GPU is memory-bandwidth
bound, so running a second model per token slows the 30B down. The draft-free
n-gram speculator is free on new text and **3.4× faster on edits**: enabled
by default.

## NPU (GenieX, Hexagon W4A16)

- NPU/Qwen3-8B:W4A16, NPU/Gemma-4-E4B-it:W4A16,
  NPU/Qwen3-0.6B:W4A16 — all chat-verified.
- Context hard-locked to **4096** by GenieX (fine for chat; unusable for
  coding agents — prompts exceed it, SDKError "Input prompt too long").
- **Qwen3 NPU models are reasoning models**: they emit 2000+ token `<think>`
  blocks before answering, which overflows the 4K ceiling mid-generation
  (same "Input prompt too long" error) — and `enable_thinking: false` is
  ignored. For NPU chat use **Gemma-4-E4B-it** (instruct, direct answers).
- NPU strength is power efficiency (long unplugged sessions), not speed vs GPU.
- First request after server start takes ~30 s (model load on demand).

## Memory footprint (48 GB machine)

| Model | llama-server working set |
|-------|--------------------------|
| Qwen3-Coder-30B Q4_0 @ ctx 16384 | ~19.4 GB |
| GPT-OSS-20B Q4_0 @ ctx 16384 | ~13 GB |
| Qwen3-8B Q5_0 @ ctx 16384 | ~6 GB |

Practical ceiling: 30B + 16K ctx needs a 32 GB machine with care (close big
apps), 48 GB comfortably.