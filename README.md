# Snapdragon NPU/GPU LLM Optimization

**Run a 30B-class coding model entirely on your Snapdragon X-series laptop — no cloud, no GPU vendor drivers, one click.**

A complete local AI stack for **Windows 11 ARM64 on Snapdragon X / X2 Elite** (Adreno GPU + Hexagon NPU), controlled from a single web panel:

- **Adreno GPU** via llama.cpp (OpenCL backend): up to **16K context**, any GGUF
- **Hexagon NPU** via Qualcomm GenieX: power-efficient W4A16 models
- **Open WebUI**: chat UI with web search, RAG, multi-user auth
- **llmnpu Panel**: one page to start/stop servers, switch models, download GGUFs, quantize, pull NPU models
- **opencode integration** (optional): terminal coding agent on the local GPU models

```
┌─────────────────────────── your browser ───────────────────────────┐
│  http://localhost:8188/panel   (toolbar + embedded Open WebUI)      │
└──────────┬───────────────────────────────┬──────────────────────────┘
           │ HTTP + WebSocket proxy        │
           ▼                               ▼
   Open WebUI (:3000, WSL)         ┌───── your Snapdragon X ─────┐
       │  ┌────────────────────────┤ llama.cpp :8081  Adreno GPU │
       │  │  OpenAI API            │ GenieX    :18181 Hexagon NPU│
       ▼  ▼                        └─────────────────────────────┘
   any OpenAI client (opencode, curl, your code)
```

## Measured performance (Snapdragon X2 Elite, 48 GB, Adreno GPU, OpenCL)

| Model | Type | Quant | tok/s (generation) | Context |
|-------|------|-------|--------------------|---------|
| **Qwen3-Coder-30B-A3B** | MoE 3B-active | Q4_0 | **~31** | 16K |
| GPT-OSS-20B | MoE | Q4_0 | ~26 | 16K |
| Qwen3-8B | dense | Q5_0 | ~22 | 16K |
| Mistral-Nemo-12B | dense | Q4_0 | ~17 | 16K |
| Qwen2.5-Coder-1.5B | dense | Q4_0 | ~90 | 16K |

The 30B MoE is the sweet spot: only ~3B parameters activate per token, so it's
**faster** than the dense 8B while being the strongest coder of the set.
CPU-only (Ollama) baselines are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md) —
the GPU gives 1.5–2.5x over CPU for most models.

## Requirements

Make sure your machine meets all three before starting — the 30B model in
particular needs the RAM and disk headroom:

- Snapdragon X / X Elite / X2 Elite laptop (Windows 11 ARM64, 24 GB+ RAM; 32 GB+ for the 30B)
- WSL2 with Ubuntu ARM64 (`wsl --install -d Ubuntu`)
- ~60 GB free disk (models + GenieX + build)

## Quickstart

```bat
REM 1. clone into your user folder
git clone https://github.com/zabojad75/Snapdragon-NPU-GPU-LLM_optimization %USERPROFILE%\llmnpu

REM 2. one-time setup
cd %USERPROFILE%\llmnpu
wsl -e bash -c "cp -r /mnt/c/Users/%USERNAME%/llmnpu ~/llmnpu && bash ~/llmnpu/scripts/wsl/setup_wsl.sh"
scripts\windows\install_geniex.bat       REM installs Qualcomm GenieX (NPU)
scripts\windows\build_opencl.bat         REM builds llama.cpp for Adreno (see docs/SETUP.md prerequisites)

REM 3. get models (~40 GB, resumable)
wsl -e bash -c "bash ~/llmnpu/scripts/wsl/download_models.sh"

REM 4. run everything
scripts\windows\start_all.bat            REM opens http://localhost:8188/panel
```

First webui login: **sign up** on the webui login screen — the first account
becomes admin.

## The panel

`http://localhost:8188/panel` — toolbar on top, Open WebUI embedded below.
One page, no terminal needed once installed: click a button, watch the
status lights confirm each action.

- **▶ Start GPU / ■ Stop** — serve any GGUF from the models dir, with context size
- **Model dropdown** — auto-scans `models/`, shows loaded model and file sizes
- **NPU toggle** — start/stop the GenieX server
- **⤓ Download** — paste any direct `.gguf` URL; resumable, auto-retry, uncapped
- **⚙ Adapt** — re-quantize GGUFs (llama-quantize) or pull Hexagon-compiled NPU models (geniex pull)
- **▤ Jobs** — live progress/logs for all running jobs
- Status lights: GPU / NPU / WebUI, polled every 2 s

The panel also transparently proxies **WebSockets** (socket.io) to Open WebUI —
without this, chat streaming silently breaks behind the proxy (see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## Docs

- [docs/SETUP.md](docs/SETUP.md) — full walkthrough from a clean Windows install (build prerequisites, GenieX, WSL venv, models)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit, NAT networking, the WebSocket bridge
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — all measurements, GPU vs CPU
- [docs/QUICKREF.md](docs/QUICKREF.md) — cheat-sheet once everything is running

## Ports

| Port | Service | Binds |
|------|---------|-------|
| 8188 | llmnpu Panel (proxy) | 127.0.0.1 |
| 3000 | Open WebUI | localhost |
| 8081 | llama.cpp GPU server (OpenAI API) | 0.0.0.0 (LAN) |
| 18181 | GenieX NPU server (OpenAI API) | 0.0.0.0 (LAN) |

## Known limits (honest engineering)

- **One GPU model at a time** — Adreno shared memory holds one model; switching = reload (~15–60 s)
- **16K context** is the practical ceiling at 32 GB RAM with the 30B model
- **NPU models are context-locked at 4096** by GenieX — fine for chat, unusable for agentic coding
- **`localhost` never works WSL→Windows** in NAT mode — always the gateway IP (scripts handle this)
- llama-quantize refuses re-quantizing already-quantized GGUFs — start from F16/BF16 sources

## License & credits

MIT for this repo's code — see [LICENSE](LICENSE), [NOTICE](NOTICE).

Built on: [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT) ·
[Qualcomm GenieX](https://github.com/qualcomm/GenieX) (BSD-3) ·
[Open WebUI](https://github.com/open-webui/open-webui) (BSD-3) ·
[opencode](https://github.com/anomalyco/opencode) (MIT).

*Snapdragon, Adreno, Hexagon and Qualcomm are trademarks of Qualcomm Technologies, Inc. This project is not affiliated with or endorsed by Qualcomm.*