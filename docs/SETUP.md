# Setup — from a clean Windows 11 ARM64 install

Tested on: Snapdragon X2 Elite, 48 GB RAM, Windows 11 (build 28000), WSL2 Ubuntu 26.04 ARM64.

Total time: ~1–2 h (dominated by the llama.cpp build and model downloads).

## 0. Prerequisites

- Windows 11 ARM64 on Snapdragon X / X Elite / X2 Elite
- 24 GB RAM minimum (32 GB+ recommended for the 30B model)
- ~60 GB free disk
- Admin rights (for installer + firewall prompts)

## 1. WSL2 + Ubuntu (ARM64)

```bat
wsl --install -d Ubuntu
```

Reboot, create your WSL user. Verify NAT mode (default):

```bash
ip route show default    # gateway like 172.x.x.1 — this is expected
```

If you later switch to mirrored networking, WSL→Windows `localhost` starts
working, but the scripts' gateway logic still functions (it's derived at runtime).

## 2. Clone the repo

```bat
git clone https://github.com/zabojad75/Snapdragon-NPU-GPU-LLM_optimization %USERPROFILE%\llmnpu
```

Copy it into WSL and run the WSL-side setup (creates venv, installs Open WebUI
+ panel deps — downloads ~1 GB of wheels):

```bat
wsl -e bash -c "cp -r /mnt/c/Users/%USERNAME%/llmnpu ~/llmnpu && bash ~/llmnpu/scripts/wsl/setup_wsl.sh"
```

## 3. llama.cpp with the Adreno OpenCL backend (the GPU engine)

One-time toolchain install on Windows:

1. **VS 2022 Build Tools** with C++ ARM64 tools
   (`Visual Studio Installer → Modify → Individual components → C++ ARM64 build tools`)
2. **LLVM** (`winget install LLVM.LLVM` → `C:\Program Files\LLVM\bin`)
3. **CMake** (`winget install Kitware.CMake`)
4. **Git for Windows** (`winget install Git.Git`)
5. **Qualcomm OpenCL SDK 2.3.2** → install to `C:\Qualcomm\OpenCL_SDK\2.3.2`
   (from Qualcomm AI Hub / developer.qualcomm.com)

Then clone + build:

```bat
cd %USERPROFILE%\llmnpu
git clone https://github.com/ggml-org/llama.cpp llama.cpp
scripts\windows\build_opencl.bat
```

Produces `%USERPROFILE%\llmnpu\pkg-opencl\bin\llama-server.exe`.

> The build uses the upstream Snapdragon preset
> (`docs/backend/snapdragon/CMakeUserPresets.json` inside llama.cpp),
> with `GGML_HEXAGON=OFF` and `LLAMA_BUILD_UI=OFF`.

## 4. GenieX (the NPU engine)

```bat
scripts\windows\install_geniex.bat
```

Downloads the official installer (pinned v0.6.1) and installs to
`%USERPROFILE%\llmnpu\geniex`. If the installer picks another location, move the
folder or set `LLMNPU_WIN_ROOT`.

Pull NPU models (W4A16, Hexagon-compiled):

```bash
geniex --data-dir "/mnt/c/Users/$WINUSER/llmnpu/geniex/models/geniex" pull qualcomm/Qwen3-8B:W4A16
geniex ... pull qualcomm/Qwen3-0.6B:W4A16
```

(or use the panel's ⚙ Adapt sheet — no CLI needed).

> NPU context is hard-locked to 4096 tokens by GenieX — great for chat,
> not for coding agents.

## 5. Models (GGUF for the GPU)

```bash
bash ~/llmnpu/scripts/wsl/download_models.sh
```

Downloads 5 curated GGUFs (~42 GB total, resumable). Or download any GGUF
yourself into `%USERPROFILE%\llmnpu\models\` — it appears in the panel automatically.

Sizing guide (Adreno uses **shared system RAM**): Q4 quant ≈ 1 GB per 1B params.
30B MoE ≈ 17 GB → needs 32 GB RAM.

## 6. Run

```bat
%USERPROFILE%\llmnpu\scripts\windows\start_all.bat
```

Opens `http://localhost:8188/panel`. On the webui login screen, **sign up** —
the first account becomes admin. Then pick a model in the panel toolbar → ▶ Start GPU.

## 7. Disable builtin tools for the NPU models (required)

Open WebUI injects ~3–4K tokens of builtin tool definitions (web search, code
interpreter, time utils, …) into every browser chat. NPU models are
context-locked at 4096 tokens, so **every** NPU chat fails with
"Input prompt too long" unless the injection is disabled per model.

Fastest path — Admin UI: **Settings → Admin → Models → click each `NPU/...`
model → turn OFF all builtin-tools toggles → save.**

SQL fallback (as the admin user, with the webui stopped):

```sql
-- meta.builtinTools all-false for each NPU model id
INSERT INTO model (id, user_id, base_model_id, name, params, meta,
                   created_at, updated_at, is_active)
VALUES ('NPU/Gemma-4-E4B-it:W4A16', '<your-user-id>', NULL,
        'NPU Gemma 4 (no tools)', '{}',
        '{"builtinTools": {"time":false,"user_input":false,"files":false,
          "knowledge":false,"chats":false,"subagents":false,"memory":false,
          "web_search":false,"image_generation":false,"code_interpreter":false,
          "notes":false,"channels":false,"tasks":false,"automations":false,
          "calendar":false,"notifications":false}}',
        strftime('%s','now'), strftime('%s','now'), 1);
-- repeat for NPU/Qwen3-8B:W4A16 and NPU/Qwen3-0.6B:W4A16, then restart webui
```

(`user_id` = your id from the `user` table. Same-model `id` merges cleanly —
no picker duplicates. GPU models keep their tools: they have 16K context.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| `localhost:8081` unreachable from WSL | Expected in NAT mode — use the gateway IP (`ip route show default`). Scripts do this automatically. |
| GPU model switch does nothing | Wait for load (15–60 s); check `%USERPROFILE%\llmnpu\logs\llama_server.log` |
| NPU server dies after ~5 min | Fixed: `serve_npu.bat` sets `--keepalive 999999` (GenieX default idle timeout was 300 s) |
| Chat freezes + "Connection lost" toasts in panel | Panel's WebSocket bridge missing (old version) — pull latest, restart panel |
| webui 401 on chat | Re-login in the webui (stale token after enabling auth) |
| OOM / swap thrash with 30B | Close big apps (llama-server holds ~19 GB); use ctx 8192; or serve a smaller model |
| Build fails on `cmake --preset` | Verify OpenCL SDK path + vcvarsall arm64; see llama.cpp `docs/backend/snapdragon/` |