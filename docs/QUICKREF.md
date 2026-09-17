# Quick reference

Everything lives under `%USERPROFILE%\llmnpu` (Windows) and `~/llmnpu` (WSL).

## Daily use

```bat
%USERPROFILE%\llmnpu\scripts\windows\start_all.bat   REM everything + opens the panel
```

Panel: `http://localhost:8188/panel` — model dropdown + ▶ Start GPU / ■ Stop,
NPU toggle, ⤓ Download, ⚙ Adapt, ▤ Jobs.

## Serve a specific model from a terminal

```bat
scripts\windows\serve_gpu.bat 30b     REM keys: 30b | gemma | 20b | 8b | 12b | 1.5b
scripts\windows\status_gpu.bat        REM what's loaded?
scripts\windows\stop_gpu.bat
```

Add a model: drop any `.gguf` into `%USERPROFILE%\llmnpu\models\` — the panel
picks it up automatically (no registration needed). For a serve key, add one
line to `serve_gpu.bat`.

## API access (OpenAI-compatible)

From Windows: `http://localhost:8081/v1` (GPU), `http://localhost:18181/v1` (NPU)
From WSL:    `http://<gateway>:8081/v1` — gateway = `ip route show default`

```bash
curl http://localhost:8081/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder-30b","messages":[{"role":"user","content":"hi"}]}'
```

Panel API from a terminal (state-changing calls need POST + the header):

```bash
curl http://127.0.0.1:8188/api/status
curl -X POST -H 'X-LLMNPU: 1' 'http://127.0.0.1:8188/api/gpu/start?model=qwen3-coder-30b-Q4_0.gguf&ctx=32768'
```

## opencode (terminal coding agent)

```bash
cp config/opencode.jsonc.example ~/.config/opencode/opencode.jsonc
bash scripts/wsl/fix_hostip.sh        # re-patches gateway IP after reboots
opencode run --model "llamacpp/qwen3-coder-30b" "your task"
```

Use the 30B MoE for real coding. Avoid geniex-npu models (4K ctx limit →
"Input prompt too long" errors).

## Big-context pipeline (inputs beyond any lane's window)

```bash
python3 scripts/wsl/context_pipeline.py --input docs/ panel/ -q "your question" \
    --out result.md --npu-cooldown 3
```

Map-reduce across lanes: chunks (default 3000 t, NPU-safe) are mapped — small
ones on the NPU (fast prefill), oversize ones on the GPU (packed, wide window) —
then the GPU model reduces the notes hierarchically into the answer.

- `--dry-run` shows the chunk/routing plan without LLM calls
- The GPU budget is read from the running server (`/props`), token counts are
  calibrated with its `/tokenize` (binary files are skipped)
- Summaries are **checkpointed** (`<out>.ckpt.jsonl`, keyed by model + prompt
  version + question + chunk): after a crash the same command reuses every
  finished chunk
- `--map-lanes gpu` maps on the 30B (slower, best quality); `--map-lanes npu`
  needs everything to fit 4K chunks
- **Phased scheduling**: NPU map runs first, GPU after — concurrent GPU+NPU
  *decode* collapses both to ~2 t/s (measured). Chunks the NPU rejects as too
  long are requeued into the GPU phase
- Transient errors (connection reset, 502/503) are retried 3× with backoff
- `--npu-cooldown 3` (default 2) rests the NPU between calls: the SoC has
  bugchecked (0x18b VSM) under sustained NPU load

## NPU models

```bash
# list / pull / remove (data-dir so panel sees them too)
geniex --data-dir "%USERPROFILE%\llmnpu\geniex\models\geniex" list
geniex ... pull qualcomm/Qwen3-8B:W4A16
```

Or panel → ⚙ Adapt → "NPU pull".

## Downloads

Panel → ⤓ Download, paste a direct `.gguf` URL (resumable, auto-retry; saved
as `.gguf.part` until complete). CLI equivalent:

```bash
curl -L --fail -C - --retry 60 --retry-all-errors -o \
  ~/llmnpu/models/model-Q4_0.gguf "https://huggingface.co/.../model-Q4_0.gguf"
```

## Reboot checklist (all automated by start_all.bat)

- Gateway IP changed → `fix_hostip.sh` + `openwebui.sh` self-heal it
- Everything is down → `start_all.bat` brings it all back
- Webui login persists (account in `~/openwebui-data/webui.db`)

## Gotchas learned the hard way

- WSL→Windows = gateway IP, **never** `localhost` (NAT mode)
- GenieX dies after 5 idle minutes unless `--keepalive` is huge
- **Both servers can stay up** — idle NPU costs ~nothing. What matters is
  which models are LOADED (geniex loads on demand): 30B GPU (~19 GB) +
  Gemma-4 NPU (~9 GB) together leave only ~6 GB free on 48 GB and Windows
  may kill something. Combos that are fine: 30B + 0.6B/8B NPU; Gemma + small
  GPU model (or GPU stopped). When in doubt, ■ Stop the GPU server (frees
  ~19 GB instantly) before a big NPU chat.
- Never restart/switch the GPU model while its file is still downloading
  (a partial .gguf fails to load)
- Chat "freezes" + "Connection lost" behind the panel = WebSocket bridge
  (fixed; keep the panel updated)
- Open WebUI launched via `wsl -e` must run from `$HOME` with
  `WEBUI_SECRET_FILE` pinned (else PermissionError on `.webui_secret_key`)
- llama-quantize refuses re-quantizing quantized GGUFs (needs F16/BF16 source)
- One GPU model at a time; 30B @ 16K ctx ≈ 19.4 GB RAM
- Draft-model speculative decoding (Qwen3-0.6B for the 30B) is **slower** on
  this hardware (12 vs 31 t/s); the built-in `--spec-type ngram-mod` is the
  one that pays off (code edits 3.4× faster) and is on by default