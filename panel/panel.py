#!/usr/bin/env python3
"""llmnpu Panel — one-page control for a Snapdragon X-series local AI stack.

Toolbar buttons + status lights on top, Open WebUI embedded below.
Controls: GPU llama.cpp server (start/stop/load/change model), NPU GenieX
server, GGUF downloads (uncapped, resumable), llama-quantize (adapt GGUFs for
GPU), geniex pull (adapt models for Hexagon NPU).

Layout
------
WSL (Ubuntu ARM64):
  <LLMNPU_ROOT>            repo + logs (default: $HOME/llmnpu)
Windows (ARM64):
  <LLMNPU_WIN_ROOT>        models, llama.cpp build, geniex, scripts (bat)
                           (default: C:\\Users\\<you>\\llmnpu, auto-detected)

Override with env vars: LLMNPU_ROOT, LLMNPU_WIN_ROOT.

Binds 127.0.0.1 only. Runs in the Open WebUI venv (fastapi+uvicorn already
there) or any venv with: fastapi, uvicorn, httpx, websockets.
"""
import asyncio, json, os, re, shlex, signal, sqlite3, subprocess, sys, threading, time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ---------------------------------------------------------------- paths/consts
WSL = Path(os.environ.get("LLMNPU_ROOT", str(Path.home() / "llmnpu"))).resolve()

def _detect_win_root() -> Path:
    """Where the Windows-side llmnpu folder lives (models, geniex, llama.cpp).

    The Windows username can differ from the WSL username, so:
    1. LLMNPU_WIN_ROOT env var (explicit override)
    2. scan C:\\Users for an existing llmnpu folder (most reliable)
    3. Windows %USERNAME% via cmd.exe
    4. fallback: profile dir matching the WSL username
    """
    if "LLMNPU_WIN_ROOT" in os.environ:
        return Path(os.environ["LLMNPU_WIN_ROOT"])
    users = Path("/mnt/c/Users")
    if users.is_dir():
        try:
            hits = [u / "llmnpu" for u in sorted(users.iterdir())
                    if u.is_dir() and (u / "llmnpu").is_dir()]
            if hits:
                return hits[0]
        except Exception:
            pass
        try:
            out = subprocess.run(["/mnt/c/Windows/System32/cmd.exe", "/c", "echo %USERNAME%"],
                                 capture_output=True, text=True, timeout=10)
            win_user = out.stdout.strip()
            if win_user and win_user != "%USERNAME%":
                return users / win_user / "llmnpu"
        except Exception:
            pass
        return users / Path.home().name / "llmnpu"
    return WSL  # no C: mount — degrade to pure-WSL layout

WIN_ROOT  = _detect_win_root()
MODELS    = WIN_ROOT / "models"
LOGS      = WSL / "logs"
PANEL     = Path(__file__).resolve().parent
GENIEX    = WIN_ROOT / "geniex" / "engines" / "geniex" / "geniex.exe"
QUANTIZER = WIN_ROOT / "llama.cpp" / "build-opencl" / "bin" / "llama-quantize.exe"
GPU_LOG   = WIN_ROOT / "logs" / "llama_server.log"
NPU_LOG   = WIN_ROOT / "logs" / "geniex_server.log"
DL_DIR    = MODELS
PORT      = 8188
GPU_PORT, NPU_PORT, WEBUI_PORT = 8081, 18181, 3000
GATEWAY   = os.popen("ip route show default | awk '{print $3}'").read().strip()

CMD = "/mnt/c/Windows/System32/cmd.exe"

# ---------------------------------------------------------------- job registry
class Job:
    """A long-running shell job (download / quantize / npu-pull) with progress."""
    def __init__(self, jid, kind, label, cmd, workdir=None, monitor=None):
        self.id, self.kind, self.label = jid, kind, label
        self.cmd, self.workdir, self.monitor = cmd, workdir, monitor
        self.started = time.time()
        self.proc = None
        self.lines = []          # last ~400 log lines
        self.status = "running"  # running|done|error
        self.result = ""

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "elapsed": int(time.time() - self.started),
                "result": self.result, "log": self.lines[-12:]}

JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

def _run_job(job: Job):
    try:
        job.proc = subprocess.Popen(job.cmd, cwd=job.workdir,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, shell=False)
        for line in job.proc.stdout:
            line = line.rstrip()
            job.lines.append(line)
            if len(job.lines) > 400:
                del job.lines[:200]
        job.proc.wait()
        job.status = "done" if job.proc.returncode == 0 else "error"
        job.result = f"exit {job.proc.returncode}"
    except Exception as e:
        job.status = "error"
        job.result = str(e)

def spawn_job(kind, label, cmd, workdir=None, monitor=None) -> Job:
    jid = f"{kind}-{int(time.time()*1000)%100000}-{len(JOBS)}"
    job = Job(jid, kind, label, cmd, workdir, monitor)
    with JOBS_LOCK:
        # one download/quantize/pull at a time per kind is enforced by callers
        JOBS[jid] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job

# ---------------------------------------------------------------- helpers
def to_win(p: Path) -> str:
    """WSL /mnt/c/... path -> C:\\... string for cmd.exe."""
    return str(p).replace("/mnt/c/", "C:\\").replace("/", "\\")

def gpu_pid():
    """PID of llama-server.exe as seen from WSL — read /proc, never spawn cmd.exe."""
    try:
        for p in Path("/proc").iterdir():
            if p.name.isdigit():
                try:
                    comm = (p / "comm").read_text().strip()
                    if comm == "llama-server.exe":
                        return int(p.name)
                except Exception:
                    continue
    except Exception:
        pass
    return None

def gpu_loaded_model():
    """Alias of the served model, from llama-server's /v1/models (OpenAI format)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{GATEWAY}:{GPU_PORT}/v1/models", timeout=3) as r:
            data = json.loads(r.read())
            return data["data"][0]["id"] if data.get("data") else None
    except Exception:
        return None

def gpu_up():
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{GATEWAY}:{GPU_PORT}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def npu_up():
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{GATEWAY}:{NPU_PORT}/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def webui_up():
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{WEBUI_PORT}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False

def kill_gpu():
    """taskkill via cmd.exe with hard timeout — run in a worker thread to never block."""
    def _kill():
        try:
            subprocess.run([CMD, "/c", "taskkill /F /IM llama-server.exe"],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    threading.Thread(target=_kill, daemon=True).start()

def kill_npu():
    def _kill():
        try:
            subprocess.run([CMD, "/c", "taskkill /F /IM geniex.exe"],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    threading.Thread(target=_kill, daemon=True).start()

def start_gpu(model_file: str, ctx: int, alias: str):
    if gpu_up():
        kill_gpu(); time.sleep(2)
    # write a one-shot launcher bat to the WINDOWS scripts dir (cmd.exe needs a C: path)
    bat = WIN_ROOT / "scripts" / "panel_gpu_launcher.bat"
    bat.parent.mkdir(parents=True, exist_ok=True)
    bat.write_text(
        "@echo off\r\n"
        f"set PATH={to_win(WIN_ROOT / 'pkg-opencl' / 'bin')};%PATH%\r\n"
        f"llama-server -m {to_win(MODELS / model_file)} "
        f"--alias {alias} -ngl 99 -c {ctx} -fa on "
        f"--host 0.0.0.0 --port {GPU_PORT} --no-webui "
        f"> {to_win(WIN_ROOT / 'logs' / 'llama_server.log')} 2>&1\r\n"
    )
    win = to_win(bat)
    def _start():
        try:
            subprocess.Popen([CMD, "/c", "start", "\"llama-server-gpu\"", "/min", win],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("gpu start failed:", e, flush=True)
    threading.Thread(target=_start, daemon=True).start()

def start_npu():
    if npu_up():
        return
    def _start():
        try:
            subprocess.Popen([CMD, "/c", to_win(WIN_ROOT / "scripts" / "serve_npu.bat")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("npu start failed:", e, flush=True)
    threading.Thread(target=_start, daemon=True).start()

# ---------------------------------------------------------------- downloads
def download_state():
    """Every .gguf in the models dir with its current size."""
    out = []
    if MODELS.is_dir():
        for f in sorted(MODELS.glob("*.gguf")):
            out.append({"name": f.name, "size": f.stat().st_size})
    return out

def curl_progress_prefix(url, out_path):
    return ["curl", "-L", "--fail", "-C", "-", "--retry", "60",
            "--retry-all-errors", "--retry-delay", "3",
            "--connect-timeout", "20",
            "-o", str(out_path), url]

# ---------------------------------------------------------------- quantize
QUANTS = ["Q4_0", "Q4_1", "Q5_0", "Q5_1", "Q8_0", "Q3_K_M", "Q4_K_M", "Q5_K_M", "Q6_K", "IQ4_XS"]

# ---------------------------------------------------------------- FastAPI app
app = FastAPI(title="llmnpu Panel")

# NOTE: "/" deliberately has NO panel route — the catch-all at the bottom proxies
# it to Open WebUI, which is what the <iframe src="/"> in /panel needs.

# ---- status
@app.get("/api/status")
def status():
    pid = gpu_pid()
    loaded = gpu_loaded_model()
    with JOBS_LOCK:
        # prune finished jobs after 10 min to keep the list tidy
        for jid in [k for k, j in JOBS.items()
                    if j.status != "running" and time.time() - j.started > 600]:
            del JOBS[jid]
        jobs = [j.to_dict() for j in JOBS.values()]
    return {
        "gpu": {"up": gpu_up(), "model": loaded, "pid": pid,
                "log_tail": _tail(GPU_LOG, 8)},
        "npu": {"up": npu_up(),
                "log_tail": _tail(NPU_LOG, 8)},
        "webui": webui_up(),
        "gateway": GATEWAY,
        "models": download_state(),
        "jobs": jobs,
        "now": time.strftime("%H:%M:%S"),
    }

def _tail(p: Path, n=8):
    try:
        return p.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []

# ---- GPU lifecycle
@app.api_route("/api/gpu/start", methods=["POST","GET"])
def gpu_start(model: str, ctx: int = 16384, alias: str = ""):
    f = MODELS / model
    if not f.exists():
        raise HTTPException(404, f"model not found: {model}")
    alias = alias or Path(model).stem
    start_gpu(model, ctx, alias)
    return {"ok": True, "model": model, "alias": alias, "ctx": ctx}

@app.api_route("/api/gpu/stop", methods=["POST","GET"])
def gpu_stop():
    kill_gpu()
    return {"ok": True}

@app.api_route("/api/npu/start", methods=["POST","GET"])
def npu_start():
    start_npu()
    return {"ok": True}

@app.api_route("/api/npu/stop", methods=["POST","GET"])
def npu_stop():
    kill_npu()
    return {"ok": True}

# ---- downloads
@app.api_route("/api/download", methods=["POST","GET"])
def download(url: str, name: str = ""):
    url = url.strip()
    if not re.match(r"^https?://", url) or ".gguf" not in url.lower():
        raise HTTPException(400, "must be a direct http(s) URL to a .gguf file")
    fname = name.strip() or url.split("/")[-1].split("?")[0]
    out = DL_DIR / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:  # one download at a time
        if any(j.kind == "download" and j.status == "running" for j in JOBS.values()):
            raise HTTPException(409, "a download is already running")
    job = spawn_job("download", f"⤓ {fname}",
                    curl_progress_prefix(url, out))
    return {"ok": True, "job": job.id, "file": fname}

# ---- quantize
@app.get("/api/quantize/targets")
def quant_targets():
    return QUANTS

@app.api_route("/api/quantize", methods=["POST","GET"])
def quantize(model: str, quant: str):
    src = MODELS / model
    if not src.exists():
        raise HTTPException(404, f"model not found: {model}")
    if quant not in QUANTS:
        raise HTTPException(400, f"unknown quant {quant}")
    stem = Path(model).stem
    out_name = f"{stem}-{quant}.gguf"
    out = MODELS / out_name
    with JOBS_LOCK:
        if any(j.kind == "quantize" and j.status == "running" for j in JOBS.values()):
            raise HTTPException(409, "a quantize job is already running")
    cmd = [to_win(QUANTIZER), to_win(src), to_win(out), quant]
    job = spawn_job("quantize", f"⚙ {out_name}", cmd)
    return {"ok": True, "job": job.id, "out": out_name}

# ---- NPU pull
@app.api_route("/api/npu/pull", methods=["POST","GET"])
def npu_pull(repo: str):
    repo = repo.strip()
    if not re.match(r"^[\w.\-]+/[\w.\-]+(:\w+)?$", repo):
        raise HTTPException(400, "bad model id, expected org/name[:precision]")
    with JOBS_LOCK:
        if any(j.kind == "npull" and j.status == "running" for j in JOBS.values()):
            raise HTTPException(409, "an NPU pull is already running")
    cmd = [CMD, "/c", to_win(GENIEX),
           "--data-dir", to_win(WIN_ROOT / "geniex" / "models" / "geniex"),
           "--skip-update", "pull", repo]
    job = spawn_job("npull", f"NPU ⤓ {repo}", cmd)
    return {"ok": True, "job": job.id}

# ---- job control
@app.get("/api/jobs")
def jobs():
    with JOBS_LOCK:
        return [j.to_dict() for j in JOBS.values()]

@app.api_route("/api/jobs/{jid}/cancel", methods=["POST","GET"])
def job_cancel(jid: str):
    with JOBS_LOCK:
        j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    if j.proc and j.proc.poll() is None:
        try:
            j.proc.terminate()
        except Exception:
            pass
        j.status = "error"; j.result = "cancelled"
    return {"ok": True}

# ---- Open WebUI embedded at the ROOT of :8188 (its absolute paths work untouched).
# Panel's own routes (/panel, /api/*) are registered above and take precedence.
import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import StreamingResponse, Response

WEBUI_BASE = f"http://127.0.0.1:{WEBUI_PORT}"
HOP = {"content-length", "transfer-encoding", "connection", "host", "keep-alive",
       "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}

@app.get("/panel", response_class=HTMLResponse)
def panel_page():
    return (PANEL / "index.html").read_text()

# ---- WebSocket bridge: forward /ws/* (socket.io) to Open WebUI.
# WebUI 0.11.3 delivers streamed chat responses via socket.io ('events' room
# emit). Without this bridge the chat freezes and the UI loops
# "Connection lost. Reconnecting..." when accessed through the panel.
async def _ws_bridge(client_ws: WebSocket, upstream_path: str):
    await client_ws.accept()
    qs = client_ws.url.query
    upstream_uri = f"ws://127.0.0.1:{WEBUI_PORT}{upstream_path}"
    if qs:
        upstream_uri += f"?{qs}"
    # copy browser cookies (auth token) upstream
    cookies = "; ".join(f"{k}={v}" for k, v in client_ws.cookies.items())
    headers = {"Cookie": cookies} if cookies else {}
    try:
        upstream_ws = await websockets.connect(upstream_uri,
                                               additional_headers=headers,
                                               max_size=2**22, ping_interval=None)
    except Exception:
        await client_ws.close(code=1011)
        return

    async def _upstream_to_client():
        try:
            async for msg in upstream_ws:
                if isinstance(msg, str):
                    await client_ws.send_text(msg)
                else:
                    await client_ws.send_bytes(msg)
        except Exception:
            pass

    async def _client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg["type"] == "websocket.receive":
                    if msg.get("text") is not None:
                        await upstream_ws.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream_ws.send(msg["bytes"])
        except Exception:
            pass

    t1 = asyncio.create_task(_upstream_to_client())
    t2 = asyncio.create_task(_client_to_upstream())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await upstream_ws.close()
    except Exception:
        pass
    try:
        await client_ws.close()
    except Exception:
        pass

@app.websocket("/ws/{path:path}")
async def ws_bridge(path: str, websocket: WebSocket):
    await _ws_bridge(websocket, f"/ws/{path}")

# NOTE: no route at "/" — the catch-all below proxies it to Open WebUI,
# which is exactly what the <iframe src="/"> needs (no recursion).

async def _proxy(request: Request):
    url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP | {"accept-encoding"}}
    body = await request.body()
    client = httpx.AsyncClient(base_url=WEBUI_BASE, timeout=600)
    try:
        req = client.build_request(request.method, url, headers=headers, content=body)
        upstream = await client.send(req, stream=True)
        enc = upstream.headers.get("content-encoding", "").lower()
        if enc:  # httpx aread() auto-decompresses; strip the stale header
            raw = await upstream.aread()
            await upstream.aclose()
            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in HOP | {"content-encoding", "content-length", "vary", "etag"}}
            return Response(content=raw, status_code=upstream.status_code,
                            headers=out_headers)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=out_headers)
    except Exception:
        await client.aclose()
        return JSONResponse({"detail": "Open WebUI not reachable on :3000"}, status_code=502)

# catch-all: everything not matched above goes to the webui
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"])
async def webui_proxy(path: str, request: Request):
    return await _proxy(request)

if __name__ == "__main__":
    print(f"llmnpu Panel on http://localhost:{PORT}/panel  (gateway {GATEWAY})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")