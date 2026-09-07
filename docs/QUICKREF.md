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
scripts\windows\serve_gpu.bat 30b     REM keys: 30b | 20b | 8b | 12b | 1.5b
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

## opencode (terminal coding agent)

```bash
cp config/opencode.jsonc.example ~/.config/opencode/opencode.jsonc
bash scripts/wsl/fix_hostip.sh        # re-patches gateway IP after reboots
opencode run --model "llamacpp/qwen3-coder-30b" "your task"
```

Use the 30B MoE for real coding. Avoid geniex-npu models (4K ctx limit →
"Input prompt too long" errors).

## NPU models

```bash
# list / pull / remove (data-dir so panel sees them too)
geniex --data-dir "%USERPROFILE%\llmnpu\geniex\models\geniex" list
geniex ... pull qualcomm/Qwen3-8B:W4A16
```

Or panel → ⚙ Adapt → "NPU pull".

## Downloads

Panel → ⤓ Download, paste a direct `.gguf` URL (resumable, auto-retry).
CLI equivalent:

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
- Chat "freezes" + "Connection lost" behind the panel = WebSocket bridge
  (fixed; keep the panel updated)
- Open WebUI launched via `wsl -e` must run from `$HOME` with
  `WEBUI_SECRET_FILE` pinned (else PermissionError on `.webui_secret_key`)
- llama-quantize refuses re-quantizing quantized GGUFs (needs F16/BF16 source)
- One GPU model at a time; 30B @ 16K ctx ≈ 19.4 GB RAM