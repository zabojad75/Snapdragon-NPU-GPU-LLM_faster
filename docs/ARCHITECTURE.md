# Architecture

## Components

```
 Windows ARM64 (host)                          WSL2 Ubuntu ARM64 (guest)
┌─────────────────────────────┐              ┌──────────────────────────────┐
│ llama-server.exe  :8081     │◄─ gateway ──│ Open WebUI      :3000        │
│ (Adreno GPU, OpenCL)         │   IP only    │ llmnpu Panel   :8188 (127.0.0.1)│
│ geniex.exe        :18181    │              │ venv: fastapi, uvicorn,     │
│ (Hexagon NPU)               │  ──────────► │        httpx, websockets    │
│ models/, geniex/,           │   localhost  │ opencode (optional)         │
│ pkg-opencl/, scripts/*.bat │   works      │ scripts/*.sh, panel/        │
└─────────────────────────────┘              └──────────────────────────────┘
```

- **GPU lane**: `llama-server` (llama.cpp, OpenCL backend, `-ngl 99 -fa on`)
  serves any GGUF at `:8081/v1` (OpenAI API). One model in memory at a time.
- **NPU lane**: `geniex serve` serves Hexagon-compiled W4A16 models at
  `:18181/v1`. Context hard-locked to 4096 by GenieX. `--keepalive 999999`
  disables its default 5-minute idle suicide.
- **WebUI**: Open WebUI in a venv inside WSL. Both backends registered as
  OpenAI connections (`sk-dummy` key). Auth enabled — accounts live in
  `~/openwebui-data/webui.db`.
- **Panel**: FastAPI app on `127.0.0.1:8188` inside the same venv.
  `/panel` serves the toolbar; everything else is reverse-proxied to Open WebUI,
  including WebSockets.

## Why a proxy at all?

Open WebUI is served at `localhost:3000` and works standalone. The panel adds:

1. **One URL** — toolbar + chat in one page, no port juggling.
2. **Server control** — start/stop/switch GPU & NPU servers, download models,
   quantize, pull NPU models — buttons instead of terminal.
3. **Path fixes for free** — the webui's absolute assets (`/_app/...`) work
   untouched because the webui is proxied at the ROOT of :8188, not a subpath.

## The two hard problems this design solves

### 1. NAT networking (WSL → Windows is NOT localhost)

In WSL2's default NAT mode, the WSL VM is behind a virtual NAT. Windows→WSL is
forwarded (`localhost:3000` works from Windows), but **WSL→Windows requires the
gateway IP** (`172.x.x.1`), which **changes on every reboot**.

All scripts derive it at runtime:

```bash
GW=$(ip route show default | awk '{print $3}')
```

- `openwebui.sh` exports `OPENAI_API_BASE_URL=http://$GW:8081/v1` **and**
  self-heals the webui's *persisted* DB connections (sqlite `openai.api_base_urls`),
  because Open WebUI caches connections across restarts.
- `fix_hostip.sh` patches `~/.config/opencode/opencode.jsonc` the same way.

**Never** hardcode `localhost:8081` in a WSL-side config — it silently fails.

### 2. Streaming behind a reverse proxy (the WebSocket bridge)

Open WebUI ≥0.11 delivers **live chat content via socket.io over WebSocket**
(`sio.emit('events', room='user:{id}')`), with `ENABLE_WEBSOCKET_SUPPORT=True`
(WebSocket-only, no polling fallback). The HTTP response body is just `null`.

A plain HTTP reverse proxy therefore breaks chat silently: the POST returns
200, the model generates fine, but **nothing streams to the browser**, and the
UI loops a *"Connection lost. Reconnecting..."* toast.

The panel fixes this with a WebSocket bridge (`@app.websocket("/ws/{path}")`):
two pump tasks pipe frames both directions between browser and webui
(cookies forwarded upstream, clean close on either side). Verified end-to-end:
a streamed chat through the panel delivers all `response:completion` deltas.

Additional proxy detail: the webui answers with **zstd/gzip** content-encoding.
The proxy strips `accept-encoding` going in; if upstream still encodes, it
reads + auto-decompresses (httpx `aread()`) and strips the stale
`content-encoding/content-length/etag/vary` headers going out.

## Data flow (a chat message through the panel)

```
browser ── POST /api/chat/completions ──► panel :8188
                                            │ HTTP proxy
                                            ▼
                                       webui :3000 (auth, tools, web search)
                                            │ OpenAI API via gateway IP
                                            ▼
                            llama-server :8081 (Adreno) or geniex :18181 (NPU)
                                            │ SSE stream
                                            ▼
                                       webui :3000
                                            │ sio.emit('events', user room)
                                            ▼
browser ◄── ws://:8188/ws/socket.io ─── panel WebSocket bridge ◄── webui
```

## Job system

Downloads / quantize / NPU pulls run as tracked jobs (threaded `subprocess`),
with live log ring-buffers, cancel support, and one-job-at-a-time per kind.
The registry is in-memory (a panel restart wipes it; `curl -C -` makes
downloads resumable).

GPU start writes a one-shot `.bat` to the **Windows** scripts dir (cmd.exe
needs a real `C:` path), then `cmd /c start /min` launches it detached.
GPU PID is read from WSL `/proc` (spawning `tasklist.exe` from a request
handler hangs).

## Startup robustness details

- `openwebui.sh` pins `WEBUI_SECRET_FILE` and `cd $HOME` — Open WebUI writes
  `.webui_secret_key` to its CWD, which explodes with PermissionError when
  launched via `wsl -e` from `C:\Windows`.
- `serve_npu.bat` passes `--keepalive 999999` — GenieX's default 300 s idle
  timeout silently kills the NPU server after 5 quiet minutes.
- All panel-side Windows process launches are detached (`cmd /c start /min`)
  and hardened with timeouts; status checks never spawn Windows processes
  from request handlers (GPU PID via `/proc`).