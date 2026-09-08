#!/bin/env python3
"""llmnpu Panel — one-page control for the local AI stack.

Toolbar buttons + status lights on top, Open WebUI embedded below.
Controls: GPU llama.cpp server (start/stop/load/change model), NPU geniex server,
GGUF downloads (uncapped, resumable), llama-quantize (adapt GGUFs for GPU),
geniex pull (adapt models for Hexagon NPU).
Binds 127.0.0.1 only. Runs in the openwebui venv (fastapi+uvicorn already there).
"""
import asyncio, json, os, re, shlex, signal, sqlite3, struct, subprocess, sys, threading, time
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

def to_win(p: Path) -> str:
    """WSL /mnt/c/... path -> C:\\... string for cmd.exe."""
    return str(p).replace("/mnt/c/", "C:\\").replace("/", "\\")

# ---------------------------------------------------------------- job registry
class Job:
    """A long-running shell job (download / quantize / npu-pull) with progress."""
    def __init__(self, jid, kind, label, cmd, workdir=None, monitor=None):
        self.id, self.kind, self.label = jid, kind, label
        self.cmd, self.workdir, self.monitor = cmd, workdir, monitor
        self.started = time.time()
        self.proc = None
        self.lines = []          # last 40 log lines
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

# ---------------------------------------------------------------- GPU control
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

def _down_wait(comm, timeout=15):
    """Synchronously taskkill a Windows process and block until it's really gone
    (so its listening port is freed before the next server binds it)."""
    try:
        subprocess.run([CMD, "/c", f"taskkill /F /IM {comm}"],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc_alive(comm) is False:
            time.sleep(0.5)   # let the OS release the socket
            return True
        time.sleep(0.5)
    return False

def kill_npu():
    def _kill():
        try:
            subprocess.run([CMD, "/c", "taskkill /F /IM geniex.exe"],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    threading.Thread(target=_kill, daemon=True).start()

def start_gpu(model_file: str, ctx: int, alias: str, my_epoch: int | None = None):
    # hot-switch safety: if a server is running (or its process is lingering),
    # kill it and WAIT until the port is freed — otherwise the new instance
    # fails to bind 8081 and dies silently
    if gpu_up() or _proc_alive("llama-server.exe") is True:
        _down_wait("llama-server.exe")
    # write a one-shot launcher bat (runs windowless via hidden.vbs — no
    # console, no taskbar entry) then spawn it detached
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
    vbs = to_win(WIN_ROOT / "scripts" / "hidden.vbs")
    def _start():
        if my_epoch is not None and ACTION_EPOCH["gpu"] != my_epoch:
            return   # a newer start/stop/restart superseded this launch
        try:
            subprocess.Popen([CMD, "/c", "wscript.exe", vbs,
                              "cmd", "/c", win],
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

# ---------------------------------------------------------------- state machine
# A background poller samples health/PID/logs every 2 s and derives a lifecycle
# state per server. /api/status reads the cache (instant, no per-request
# timeouts, no false "down" flicker). Intent tracking lets us distinguish
# "starting" (we asked) from "crashed" (it died on its own).
#
# Crash detection is two-signal and debounced: a server is only declared
# crashed after 3 consecutive failed health checks AND a stale log (>10 s) AND
# no live process. A busy server (slow health check but growing log / live
# process) stays "busy" — never a false "crashed".
STATE = {"gpu": {"state": "off", "since": 0, "prev": "off", "detail": "", "fails": 0, "saw_up": False},
         "npu": {"state": "off", "since": 0, "prev": "off", "detail": "", "fails": 0, "saw_up": False}}
INTENT = {"gpu": None, "npu": None}   # {"action": "start"/"stop"/"restart", "model":..., "since": ts}
CACHE = {}
CACHE_LOCK = threading.Lock()

# monotonically increasing per-lane action counter — a restart worker aborts if
# a newer start/stop/restart arrived during its kill+wait, so a Stop can never
# be overridden by an in-flight restart
ACTION_EPOCH = {"gpu": 0, "npu": 0}

def _bump_action(which):
    ACTION_EPOCH[which] += 1
    STATE[which]["saw_up"] = False   # a fresh user action resets crash evidence
    return ACTION_EPOCH[which]

# expected sizes for curated GGUFs (bytes) — used to flag partial downloads
EXPECTED = {"qwen3-coder-30b-Q4_0.gguf": 17379990688}

CRASH_FAILS = 3      # consecutive failed samples before declaring crashed
LOG_STALE_S = 10     # log older than this (with health failing) counts as dead

def _sample_http(url, timeout=3):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def _proc_alive(comm):
    """Is a Windows process with this image name running? (tasklist, ~120 ms)."""
    try:
        out = subprocess.run(
            [CMD, "/c", "tasklist", "/FI", f"IMAGENAME eq {comm}", "/NH"],
            capture_output=True, text=True, timeout=10)
        return comm.lower() in out.stdout.lower()
    except Exception:
        return None   # unknown — don't treat as dead

def _log_info(p):
    try:
        st = p.stat()
        return st.st_size, st.st_mtime
    except Exception:
        return 0, 0

def _gpu_toks():
    try:
        for ln in reversed(GPU_LOG.read_text(errors="replace").splitlines()):
            m = re.search(r"tg = (\d+\.\d+) t/s", ln)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None

def _gpu_ctx_from_log():
    """ctx the running llama-server was started with (from its load banner)."""
    try:
        for ln in GPU_LOG.read_text(errors="replace").splitlines():
            m = re.search(r"n_ctx_slot = (\d+)", ln)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None

def _npu_loaded():
    try:
        for ln in reversed(NPU_LOG.read_text(errors="replace").splitlines()):
            if "POST" in ln and "chat/completions" in ln:
                return True
    except Exception:
        pass
    return False

def _free_ram_gb():
    try:
        out = subprocess.run(
            ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
             "-NoProfile", "-Command",
             "[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)"],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())
    except Exception:
        return None

def _npu_busy(window=60):
    """NPU counts as 'doing work' if a chat completion OR a slow (>1.5 s) request
    landed within the window. The panel's own fast health-check GETs are ignored,
    so an idle server reads 'ready', not 'busy'."""
    try:
        lines = NPU_LOG.read_text(errors="replace").splitlines()
    except Exception:
        return False
    if not lines:
        return False
    now = time.time()
    pat = re.compile(
        r"\[GIN\] (\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2}) \| \d+ \| +([0-9.]+)([µmns]?)s +\| .* \| +(GET|POST) +\"([^\"]+)\"")
    for ln in reversed(lines[-200:]):
        m = pat.search(ln)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y/%m/%d - %H:%M:%S"))
        except Exception:
            continue
        if now - ts > window:
            return False            # lines only get older from here
        dur = float(m.group(2))
        unit = m.group(3)
        if unit == "\u00b5" or unit == "u":
            dur /= 1e6
        elif unit == "m":
            dur /= 1e3
        elif unit == "n":
            dur /= 1e9
        method, path = m.group(4), m.group(5)
        if "chat/completions" in path or dur > 1.5:
            return True
    return False

def _set_state(which, new, detail):
    with CACHE_LOCK:
        cur = STATE[which]
        if cur["state"] != new:
            cur["prev"] = cur["state"]
            cur["state"] = new
            cur["since"] = time.time()
        cur["detail"] = detail
    return cur["state"], cur["since"], detail

def _derive(which, up, busy, log_mtime, now, proc_alive):
    """Two-signal, debounced lifecycle derivation for a server lane.

    States: off / starting / ready / busy / stopping / crashed.
    'busy' means doing real work (caller passes it). Crash is only declared for
    a server we SAW alive after the last user action (saw_up) that then
    vanished — a server cleanly stopped (or never started) stays 'off'.
    """
    st = STATE[which]
    intent = INTENT[which]
    prev = st["state"]
    log_fresh = (now - log_mtime) < LOG_STALE_S

    if up:
        st["fails"] = 0
        st["saw_up"] = True
        if intent and intent["action"] in ("start", "restart"):
            INTENT[which] = None   # launch confirmed
        elif intent and intent["action"] == "stop":
            # still up but stop requested (taskkill in flight) -> keep intent,
            # show 'stopping'; the OFF transition happens once it actually dies
            return _set_state(which, "stopping", "stopping…")
        return _set_state(which, "busy" if busy else "ready",
                          "serving" if busy else "idle")

    # not up
    if intent and intent["action"] in ("start", "restart") and (now - intent["since"]) < 60:
        return _set_state(which, "starting", "launching…")
    if intent and intent["action"] == "stop":
        INTENT[which] = None
        return _set_state(which, "off", "stopped")

    # health failed but process present -> alive, just slow. A fresh log alone
    # only counts as alive if we saw the server up (saw_up); after a clean stop
    # saw_up is False so a leftover fresh log tail must NOT resurrect 'busy'.
    if proc_alive is True or (st["saw_up"] and log_fresh):
        st["fails"] = 0
        return _set_state(which, "busy", "busy · health slow")

    # no log activity, no process: candidate dead — only a crash if we saw it
    # alive since the last user action, else it's simply off
    st["fails"] += 1
    if st["fails"] >= CRASH_FAILS:
        if st["saw_up"]:
            INTENT[which] = None
            return _set_state(which, "crashed", "process died — restart")
        # never saw it up since the last action -> clean off, not a crash
        st["fails"] = 0
        st["saw_up"] = False
        return _set_state(which, "off", "stopped")
    # < 3 fails: keep last live state briefly, but never flip a clean stop
    if prev in ("ready", "busy", "stopping", "starting"):
        if intent is None and not st["saw_up"] and not log_fresh:
            return _set_state(which, "off", "stopped")
        return _set_state(which, prev, "unreachable…")
    return _set_state(which, "off", "stopped")

def _derive_gpu(up, busy, log_mtime, now, proc_alive):
    return _derive("gpu", up, busy, log_mtime, now, proc_alive)

def _derive_npu(up, busy, log_mtime, now, proc_alive):
    return _derive("npu", up, busy, log_mtime, now, proc_alive)

# ---------------------------------------------------------------- RAM estimator
# VRAM (= shared system RAM on Adreno) needed by a GGUF at a given context:
#   weights + kv_cache + ~2.5 GB llama-server overhead.
# KV bytes/token parsed from the GGUF header: layers × kv_heads × head_dim,
# each fp16 (2 bytes) for K and V.
_GGUF_CACHE = {}

def _gguf_kv_per_token(path):
    """Bytes of KV cache per token, parsed from the GGUF header (cached).

    GGUF v3: scalar types 0-7,10-12; 8=string (u64 len); 9=array
    (u32 elem_type, u64 count, then raw elements).
    """
    if path in _GGUF_CACHE:
        return _GGUF_CACHE[path]
    val = None
    SIZES = {0:1, 1:1, 2:2, 3:2, 4:4, 5:4, 6:4, 7:1, 10:8, 11:8, 12:8}
    FMTS  = {0:"<B", 1:"<b", 2:"<H", 3:"<h", 4:"<I", 5:"<i", 6:"<f", 7:"<B", 10:"<Q", 11:"<q", 12:"<d"}
    def _read_value(f, t):
        if t == 8:
            n, = struct.unpack("<Q", f.read(8)); return f.read(n)
        if t == 9:
            et, = struct.unpack("<I", f.read(4))
            n,  = struct.unpack("<Q", f.read(8))
            if et == 8:
                for _ in range(n):
                    sl, = struct.unpack("<Q", f.read(8)); f.read(sl)
            else:
                f.read(n * SIZES[et])
            return None
        return struct.unpack(FMTS[t], f.read(SIZES[t]))[0]
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                raise ValueError("not a GGUF")
            ver, = struct.unpack("<I", f.read(4))
            def rd(n=4):
                return struct.unpack("<Q" if n == 8 else "<I", f.read(n))[0]
            rd(8 if ver >= 3 else 4)                    # n_tensors
            n_kv = rd(8 if ver >= 3 else 4)
            hdr = {}
            for _ in range(n_kv):
                klen, = struct.unpack("<Q", f.read(8))
                key = f.read(klen).decode()
                t, = struct.unpack("<I", f.read(4))
                hdr[key] = _read_value(f, t)
            arch = (hdr.get("general.architecture") or b"").decode().lower()
            layers = hdr.get(f"{arch}.block_count") or 0
            heads  = hdr.get(f"{arch}.attention.head_count_kv") or 0
            kvh = hdr.get(f"{arch}.attention.key_length")
            if kvh is None and heads:
                emb = hdr.get(f"{arch}.embedding_length") or 0
                hc  = hdr.get(f"{arch}.attention.head_count") or 0
                kvh = emb // hc if hc else 0     # default head dim
            if layers and heads and kvh:
                val = layers * kvh * heads * 2 * 2   # K+V, fp16
    except Exception:
        val = None
    _GGUF_CACHE[path] = val
    return val

def _gpu_ram_need(model_file, ctx):
    """Estimate RAM (GB) needed to serve model_file at ctx tokens."""
    try:
        weights = (MODELS / model_file).stat().st_size
    except Exception:
        return None
    kvpt = _gguf_kv_per_token(MODELS / model_file)
    if kvpt is None:
        return None
    return (weights + kvpt * ctx) / 1e9 + 2.5   # + llama-server overhead

# the ctx selected at the last GPU launch (for the UI's RAM hint)
LAST_GPU = {"model": None, "ctx": None}


def _poller():
    last_ram = 0.0
    while True:
        now = time.time()
        # process check is fast (~0.12 s); only run the slow health check when
        # the process is alive (a dead port otherwise eats the full timeout)
        gproc = _proc_alive("llama-server.exe")
        gup = _sample_http(f"http://{GATEWAY}:{GPU_PORT}/health", 3) if gproc is not False else False
        gsize, gmtime = _log_info(GPU_LOG)
        gmodel = gpu_loaded_model() if gup else None
        gtoks = _gpu_toks() if gup else None
        # adopt the running model's ctx from its log (server prints it at load)
        if gup and gmodel and LAST_GPU["model"] is None:
            f = MODELS / gmodel
            if not f.exists():
                cands = [x for x in MODELS.glob("*.gguf") if x.stem.startswith(gmodel)]
                f = cands[0] if cands else f
            LAST_GPU["model"] = f.name
            LAST_GPU["ctx"] = _gpu_ctx_from_log() or 16384
        gbusy = gup and (now - gmtime) < 3   # llama log grows only during inference
        gstate, gsince, gdetail = _derive_gpu(gup, gbusy, gmtime, now, gproc)

        nproc = _proc_alive("geniex.exe")
        nup = _sample_http(f"http://{GATEWAY}:{NPU_PORT}/v1/models", 3) if nproc is not False else False
        nsize, nmtime = _log_info(NPU_LOG)
        nloaded = _npu_loaded() if nup else False
        nbusy = nup and _npu_busy()
        nstate, nsince, ndetail = _derive_npu(nup, nbusy, nmtime, now, nproc)

        wup = _sample_http(f"http://127.0.0.1:{WEBUI_PORT}/health", 3)

        with CACHE_LOCK:
            CACHE.update({
                "gpu": {"up": gup, "model": gmodel, "toks": gtoks,
                        "log_size": gsize, "log_mtime": gmtime,
                        "state": gstate, "since": gsince, "detail": gdetail},
                "npu": {"up": nup, "loaded": nloaded,
                        "log_size": nsize, "log_mtime": nmtime,
                        "state": nstate, "since": nsince, "detail": ndetail},
                "webui": wup,
                "ram_free": last_ram,
                "now": time.strftime("%H:%M:%S"),
            })
        if now - last_ram > 30:
            last_ram = _free_ram_gb() or last_ram
        time.sleep(2)

def _start_poller():
    t = threading.Thread(target=_poller, daemon=True)
    t.start()

# ---------------------------------------------------------------- downloads
def download_state():
    """Every .gguf in the models dir with size + expected size (for partial %)."""
    out = []
    for f in sorted(DL_DIR.glob("*.gguf")):
        size = f.stat().st_size
        exp = EXPECTED.get(f.name)
        out.append({"name": f.name, "size": size, "expected": exp})
    return out

def hf_size(url):
    import urllib.request
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", "curl/8")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0

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
    with JOBS_LOCK:
        # prune finished jobs after 10 min to keep the list tidy
        for jid in [k for k, j in JOBS.items()
                    if j.status != "running" and time.time() - j.started > 600]:
            del JOBS[jid]
        jobs = [j.to_dict() for j in JOBS.values()]
    with CACHE_LOCK:
        c = dict(CACHE)
    g = c.get("gpu", {}); n = c.get("npu", {})
    return {
        "gpu": {"up": g.get("up", False), "model": g.get("model"),
                "state": g.get("state", "off"), "since": g.get("since", 0),
                "detail": g.get("detail", ""), "toks": g.get("toks"),
                "log_tail": _tail(GPU_LOG, 8)},
        "npu": {"up": n.get("up", False), "loaded": n.get("loaded", False),
                "state": n.get("state", "off"), "since": n.get("since", 0),
                "detail": n.get("detail", ""),
                "log_tail": _tail(NPU_LOG, 8)},
        "webui": c.get("webui", False),
        "ram_free": c.get("ram_free"),
        "gpu_ram": _gpu_ram_need(LAST_GPU["model"], LAST_GPU["ctx"]) if LAST_GPU["model"] else None,
        "gateway": GATEWAY,
        "models": download_state(),
        "jobs": jobs,
        "now": c.get("now", time.strftime("%H:%M:%S")),
    }

# ---- ctx RAM estimate: given model + ctx, how much RAM will it need?
@app.get("/api/ctx/estimate")
def ctx_estimate(model: str, ctx: int):
    f = MODELS / model
    if not f.exists():
        raise HTTPException(404, f"model not found: {model}")
    need = _gpu_ram_need(model, int(ctx))
    if need is None:
        return {"model": model, "ctx": int(ctx), "need_gb": None, "ok": None}
    return {"model": model, "ctx": int(ctx), "need_gb": round(need, 1)}


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
    # refuse partial downloads so a half-written .gguf is never loaded
    exp = EXPECTED.get(model)
    if exp and f.stat().st_size < exp:
        raise HTTPException(409, f"model still downloading ({f.stat().st_size/1e9:.1f}/{exp/1e9:.1f} GB) — wait for it to finish")
    alias = alias or Path(model).stem
    INTENT["gpu"] = {"action": "start", "model": model, "since": time.time()}
    LAST_GPU["model"], LAST_GPU["ctx"] = model, int(ctx)
    my_epoch = _bump_action("gpu")
    start_gpu(model, ctx, alias, my_epoch)
    return {"ok": True, "model": model, "alias": alias, "ctx": ctx}

@app.api_route("/api/gpu/stop", methods=["POST","GET"])
def gpu_stop():
    INTENT["gpu"] = {"action": "stop", "since": time.time()}
    _bump_action("gpu")
    kill_gpu()
    return {"ok": True}

@app.api_route("/api/gpu/restart", methods=["POST","GET"])
def gpu_restart(model: str = "", ctx: int = 16384, alias: str = ""):
    # restart the currently loaded model, or the given one
    target = model or (gpu_loaded_model() or "")
    if not target:
        raise HTTPException(400, "no model loaded and none given")
    f = MODELS / target
    if not f.exists():
        # allow restart by alias (e.g. qwen3-coder-30b) -> find the file
        cands = [x for x in MODELS.glob("*.gguf") if x.stem.startswith(target)]
        if not cands:
            raise HTTPException(404, f"model not found: {target}")
        f = cands[0]
    exp = EXPECTED.get(f.name)
    if exp and f.stat().st_size < exp:
        raise HTTPException(409, f"model still downloading ({f.stat().st_size/1e9:.1f}/{exp/1e9:.1f} GB)")
    INTENT["gpu"] = {"action": "restart", "model": f.name, "since": time.time()}
    LAST_GPU["model"], LAST_GPU["ctx"] = f.name, int(ctx)
    my_epoch = _bump_action("gpu")
    start_gpu(f.name, ctx, alias or Path(f.name).stem, my_epoch)
    return {"ok": True, "model": f.name, "alias": alias or Path(f.name).stem, "ctx": ctx}

@app.api_route("/api/models/delete", methods=["POST","GET"])
def model_delete(model: str):
    name = (model or "").strip()
    if (not name.lower().endswith(".gguf") or Path(name).name != name
            or "/" in name or "\\" in name):
        raise HTTPException(400, "bad model filename")
    f = MODELS / name
    if not f.exists() or not f.is_file():
        raise HTTPException(404, f"model not found: {name}")
    # the served alias often differs from the filename (alias qwen3-coder-30b
    # vs file qwen3-coder-30b-Q4_0.gguf) — match on either direction so the
    # loaded model can never be deleted out from under the server
    stem = Path(name).stem
    loaded = gpu_loaded_model()
    if gpu_up():
        if not loaded:
            raise HTTPException(409, "GPU server is up but loaded model is unknown — stop it first")
        if (loaded in (name, stem) or stem.startswith(loaded)
                or name.startswith(loaded) or loaded.startswith(stem)):
            raise HTTPException(409, "model is currently loaded — stop the GPU server first")
    try:
        f.unlink()
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"ok": True, "deleted": name}

@app.api_route("/api/npu/start", methods=["POST","GET"])
def npu_start():
    INTENT["npu"] = {"action": "start", "since": time.time()}
    _bump_action("npu")
    start_npu()
    return {"ok": True}

@app.api_route("/api/npu/stop", methods=["POST","GET"])
def npu_stop():
    INTENT["npu"] = {"action": "stop", "since": time.time()}
    _bump_action("npu")
    kill_npu()
    return {"ok": True}

@app.api_route("/api/npu/restart", methods=["POST","GET"])
def npu_restart():
    # unconditional kill-then-start: works whether the server is healthy,
    # half-dead, or already gone (the toggle's stale-state trap)
    INTENT["npu"] = {"action": "restart", "since": time.time()}
    my_epoch = _bump_action("npu")
    def _restart():
        _down_wait("geniex.exe")
        if ACTION_EPOCH["npu"] != my_epoch:
            return   # a newer start/stop/restart superseded this restart
        # start_npu() early-returns when up; call the launcher directly
        try:
            subprocess.Popen([CMD, "/c", to_win(WIN_ROOT / "scripts" / "serve_npu.bat")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("npu restart failed:", e, flush=True)
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True}

# ---- logs
@app.get("/api/logs/{which}")
def logs(which: str, n: int = 50):
    p = {"gpu": GPU_LOG, "npu": NPU_LOG}.get(which)
    if not p:
        raise HTTPException(404, "unknown log")
    return {"log": _tail(p, n)}

# ---- downloads
@app.api_route("/api/download", methods=["POST","GET"])
def download(url: str, name: str = ""):
    url = url.strip()
    if not re.match(r"^https?://", url) or ".gguf" not in url.lower():
        raise HTTPException(400, "must be a direct http(s) URL to a .gguf file")
    fname = name.strip() or url.split("/")[-1].split("?")[0]
    out = DL_DIR / fname
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
    if Path(model).stem.endswith(tuple(q.lower() for q in QUANTS)):
        pass  # re-quantizing a quant is allowed (user's choice)
    cmd = [str(QUANTIZER), str(src).replace("/mnt/c/", "C:\\").replace("/", "\\"),
           str(out).replace("/mnt/c/", "C:\\").replace("/", "\\"), quant]
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
    cmd = [CMD, "/c", str(GENIEX).replace("/mnt/c/", "C:\\").replace("/", "\\"),
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
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, Response, RedirectResponse
import websockets

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
        # upstream = websockets lib: send() accepts str|bytes; forward as-is
        try:
            async for msg in upstream_ws:
                if isinstance(msg, str):
                    await client_ws.send_text(msg)
                else:
                    await client_ws.send_bytes(msg)
        except Exception:
            pass

    async def _client_to_upstream():
        # client = Starlette: raw ASGI receive, handles text+binary
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

    import asyncio as _aio
    t1 = _aio.create_task(_upstream_to_client())
    t2 = _aio.create_task(_client_to_upstream())
    done, pending = await _aio.wait({t1, t2}, return_when=_aio.FIRST_COMPLETED)
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
    _start_poller()
    print(f"llmnpu Panel on http://localhost:{PORT}/panel  (gateway {GATEWAY})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")