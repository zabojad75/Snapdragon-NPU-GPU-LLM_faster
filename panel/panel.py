#!/bin/env python3
"""llmnpu Panel — one-page control for the local AI stack.

Toolbar buttons + status lights on top, Open WebUI embedded below.
Controls: GPU llama.cpp server (start/stop/load/change model), NPU geniex server,
GGUF downloads (uncapped, resumable), llama-quantize (adapt GGUFs for GPU),
geniex pull (adapt models for Hexagon NPU).
Binds 127.0.0.1 only. Runs in the openwebui venv (fastapi+uvicorn already there).

Security model: the panel's own /api/* routes only answer requests whose Host
is the panel itself (blocks DNS rebinding), and every state-changing route is
POST-only and requires the `X-LLMNPU: 1` header. A cross-site page cannot send
a custom header without a CORS preflight, which the panel never grants — so a
web page cannot start servers, download files or delete models. User input
that reaches a cmd.exe command line (model file, alias) is allowlist-validated.
"""
import json, os, re, struct, subprocess, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
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
PANEL     = Path(__file__).resolve().parent
GENIEX    = WIN_ROOT / "geniex" / "engines" / "geniex" / "geniex.exe"
QUANTIZER = WIN_ROOT / "llama.cpp" / "build-opencl" / "bin" / "llama-quantize.exe"
GPU_LOG   = WIN_ROOT / "logs" / "llama_server.log"
NPU_LOG   = WIN_ROOT / "logs" / "geniex_server.log"
EXPECTED_FILE = MODELS / ".expected.json"
PORT      = int(os.environ.get("LLMNPU_PANEL_PORT", "8188"))
GPU_PORT, NPU_PORT, WEBUI_PORT = 8081, 18181, 3000
GATEWAY   = os.popen("ip route show default | awk '{print $3}'").read().strip()

# llama-server tuning for Snapdragon X2 Elite + Adreno, measured 2026-09-17
# (docs/BENCHMARKS.md, "Server tuning"):
#   -t 4                   with every layer on the GPU, 4 CPU threads are as fast
#                          as 18 (30B pp512 525 vs 502, tg 35.6 vs 34.6) for less
#                          CPU power/heat
#   --spec-type ngram-mod  draft-free speculative decoding from repeated n-grams:
#                          code edits 25 -> 87 t/s on the 30B, fresh text unchanged
GPU_TUNING = "-t 4 --spec-type ngram-mod"

# Per-model extra flags, matched on the file name prefix (case-insensitive).
# Gemma 4 thinks by default; for translations/summaries the hidden reasoning
# ate the whole output budget (empty answers), so thinking is off. Only
# allowlisted literals go into the .bat — never user input.
GPU_MODEL_ARGS = [
    ("gemma-4", "--reasoning off"),
]

def model_args(model_file: str) -> str:
    name = model_file.lower()
    return " ".join(a for prefix, a in GPU_MODEL_ARGS if name.startswith(prefix))

CMD = "/mnt/c/Windows/System32/cmd.exe"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

def to_win(p: Path) -> str:
    """WSL /mnt/c/... path -> C:\\... string for cmd.exe."""
    return str(p).replace("/mnt/c/", "C:\\").replace("/", "\\")

def win_script(name: str) -> Path:
    """Windows-side script: repo layout (scripts\\windows\\) or flat (scripts\\)."""
    for d in (WIN_ROOT / "scripts" / "windows", WIN_ROOT / "scripts"):
        if (d / name).exists():
            return d / name
    return WIN_ROOT / "scripts" / "windows" / name

# ---------------------------------------------------------------- input validation
# Anything below ends up on a cmd.exe command line or in a .bat file, where
# & | < > ^ % " ( ) and spaces are metacharacters — allowlist, never escape.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.gguf$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
CTX_MIN, CTX_MAX = 512, 262144

def _safe_model_name(name: str) -> str:
    name = (name or "").strip()
    if not MODEL_RE.match(name) or len(name) > 200:
        raise HTTPException(400, "bad model filename (letters, digits, . _ + - and .gguf only)")
    return name

def _model_path(name: str) -> Path:
    """Validated path of an existing model file inside MODELS."""
    f = MODELS / _safe_model_name(name)
    if not f.is_file():
        if (MODELS / (f.name + ".part")).exists():
            raise HTTPException(409, f"{f.name} is still downloading")
        raise HTTPException(404, f"model not found: {f.name}")
    return f

def _safe_alias(alias: str, model_file: str) -> str:
    alias = (alias or "").strip() or default_alias(model_file)
    if not ALIAS_RE.match(alias):
        raise HTTPException(400, "bad alias (letters, digits, . _ : - only, max 64)")
    return alias

def _safe_ctx(ctx) -> int:
    try:
        ctx = int(ctx)
    except (TypeError, ValueError):
        raise HTTPException(400, "ctx must be an integer")
    if not CTX_MIN <= ctx <= CTX_MAX:
        raise HTTPException(400, f"ctx must be between {CTX_MIN} and {CTX_MAX}")
    return ctx

_QUANT_SUFFIX = re.compile(r"-(I?Q\d\w*|F16|BF16|F32)$", re.IGNORECASE)

def default_alias(model_file: str) -> str:
    """Served model id: filename stem without the quant suffix
    (qwen3-coder-30b-Q4_0.gguf -> qwen3-coder-30b), matching serve_gpu.bat
    and the opencode config, so the id is the same whichever launcher ran."""
    return _QUANT_SUFFIX.sub("", Path(model_file).stem) or Path(model_file).stem

# ---------------------------------------------------------------- request guards
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

def guard_host(request: Request):
    """Only answer requests addressed to the panel itself (DNS rebinding)."""
    if request.headers.get("host", "").lower() not in ALLOWED_HOSTS:
        raise HTTPException(403, "forbidden host")

def guard_write(request: Request):
    """State-changing routes: POST + custom header (forces a CORS preflight,
    so cross-site pages can't trigger them)."""
    if request.method != "POST" or request.headers.get("x-llmnpu") != "1":
        raise HTTPException(403, "state-changing call needs POST + X-LLMNPU: 1 header")

# ---------------------------------------------------------------- log helpers
def _read_tail(p: Path, max_bytes=65536):
    """Last lines of a file without reading all of it (logs live on /mnt/c,
    where full reads on every poll are slow)."""
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except Exception:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # first line is probably cut
    return lines

def _read_head(p: Path, max_bytes=262144):
    try:
        with open(p, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []

def _tail(p: Path, n=8):
    return _read_tail(p)[-n:]

def _load_expected():
    """Expected full sizes of GGUFs (bytes), recorded at download start."""
    exp = {"qwen3-coder-30b-Q4_0.gguf": 17379990688}
    try:
        exp.update(json.loads(EXPECTED_FILE.read_text()))
    except Exception:
        pass
    return exp

def _save_expected(name, size):
    exp = {}
    try:
        exp = json.loads(EXPECTED_FILE.read_text())
    except Exception:
        pass
    if size:
        exp[name] = size
    else:
        exp.pop(name, None)
    try:
        EXPECTED_FILE.write_text(json.dumps(exp, indent=1))
    except Exception as e:
        print("could not record expected size:", e, flush=True)

def _check_complete(f: Path):
    exp = _load_expected().get(f.name)
    size = f.stat().st_size
    if exp and size < exp:
        raise HTTPException(409, f"model still downloading ({size/1e9:.1f}/{exp/1e9:.1f} GB) — wait for it to finish")

# ---------------------------------------------------------------- job registry
class Job:
    """A long-running shell job (download / quantize / npu-pull) with progress."""
    def __init__(self, jid, kind, label, cmd, on_done=None):
        self.id, self.kind, self.label = jid, kind, label
        self.cmd, self.on_done = cmd, on_done
        self.started = time.time()
        self.proc = None
        self.lines = []          # recent log lines
        self.status = "running"  # running|done|error
        self.result = ""
        self.cancelled = False

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "elapsed": int(time.time() - self.started),
                "result": self.result, "log": self.lines[-12:]}

JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

def _run_job(job: Job):
    try:
        job.proc = subprocess.Popen(job.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace")
        for line in job.proc.stdout:
            job.lines.append(line.rstrip())
            if len(job.lines) > 400:
                del job.lines[:200]
        job.proc.wait()
        if job.cancelled:
            job.status, job.result = "error", "cancelled"
            return
        job.status = "done" if job.proc.returncode == 0 else "error"
        job.result = f"exit {job.proc.returncode}"
        if job.status == "done" and job.on_done:
            job.result = job.on_done() or job.result
    except Exception as e:
        job.status = "error"
        job.result = str(e)

def spawn_job(kind, label, cmd, on_done=None) -> Job:
    jid = f"{kind}-{int(time.time()*1000)%100000}-{len(JOBS)}"
    job = Job(jid, kind, label, cmd, on_done)
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job

def _job_running(kind):
    with JOBS_LOCK:
        return any(j.kind == kind and j.status == "running" for j in JOBS.values())

# ---------------------------------------------------------------- GPU/NPU control
def _http_ok(url, timeout=3):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

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
    return _http_ok(f"http://{GATEWAY}:{GPU_PORT}/health", 2)

def npu_up():
    return _http_ok(f"http://{GATEWAY}:{NPU_PORT}/v1/models", 2)

def _taskkill_async(image):
    """taskkill via cmd.exe with hard timeout, in a worker thread (never blocks)."""
    def _kill():
        try:
            subprocess.run([CMD, "/c", f"taskkill /F /IM {image}"],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    threading.Thread(target=_kill, daemon=True).start()

def _procs_alive(images):
    """{image: True/False/None} from ONE tasklist call (None = unknown).
    Each cmd.exe launch from WSL costs ~0.1 s and a Windows process creation,
    so the poller checks all images at once instead of one call per image."""
    try:
        out = subprocess.run([CMD, "/c", "tasklist", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, errors="replace", timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return {i: None for i in images}
        running = {ln.split(",", 1)[0].strip('"').lower()
                   for ln in out.stdout.splitlines() if ln.strip()}
        return {i: i.lower() in running for i in images}
    except Exception:
        return {i: None for i in images}

def _proc_alive(image):
    return _procs_alive([image])[image]

def _down_wait(image, timeout=15):
    """Synchronously taskkill a Windows process and block until it's really gone
    (so its listening port is freed before the next server binds it)."""
    try:
        subprocess.run([CMD, "/c", f"taskkill /F /IM {image}"],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc_alive(image) is False:
            time.sleep(0.5)   # let the OS release the socket
            return True
        time.sleep(0.5)
    return False

def _launch_hidden(target: Path):
    """Run a .bat windowless via hidden.vbs (no console, no taskbar entry)."""
    subprocess.Popen([CMD, "/c", "wscript.exe", to_win(win_script("hidden.vbs")),
                      "cmd", "/c", to_win(target)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def start_gpu(model_file: str, ctx: int, alias: str, my_epoch: int):
    """model_file / alias / ctx MUST already be validated (_model_path,
    _safe_alias, _safe_ctx): they are written into a .bat file."""
    def _start():
        # hot-switch safety: if a server is running (or its process is lingering),
        # kill it and WAIT until the port is freed — otherwise the new instance
        # fails to bind 8081 and dies silently
        if gpu_up() or _proc_alive("llama-server.exe") is True:
            _down_wait("llama-server.exe")
        if ACTION_EPOCH["gpu"] != my_epoch:
            return   # a newer start/stop/restart superseded this launch
        bat = win_script("hidden.vbs").parent / "panel_gpu_launcher.bat"
        bat.write_text(
            "@echo off\r\n"
            f"set PATH={to_win(WIN_ROOT / 'pkg-opencl' / 'bin')};%PATH%\r\n"
            f"llama-server -m \"{to_win(MODELS / model_file)}\" "
            f"--alias {alias} -ngl 99 -c {ctx} -fa on {GPU_TUNING} {model_args(model_file)} "
            f"--host 0.0.0.0 --port {GPU_PORT} --no-webui "
            f"> \"{to_win(WIN_ROOT / 'logs' / 'llama_server.log')}\" 2>&1\r\n"
        )
        try:
            _launch_hidden(bat)
        except Exception as e:
            print("gpu start failed:", e, flush=True)
    threading.Thread(target=_start, daemon=True).start()

def _launch_npu():
    try:
        subprocess.Popen([CMD, "/c", to_win(win_script("serve_npu.bat"))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("npu start failed:", e, flush=True)

# ---------------------------------------------------------------- state machine
# A background poller samples health/PID/logs and derives a lifecycle state per
# server. /api/status reads the cache (instant, no per-request timeouts, no
# false "down" flicker). Intent tracking lets us distinguish "starting" (we
# asked) from "crashed" (it died on its own).
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

# monotonically increasing per-lane action counter — a start/restart worker
# aborts if a newer start/stop/restart arrived during its kill+wait, so a Stop
# can never be overridden by an in-flight restart
ACTION_EPOCH = {"gpu": 0, "npu": 0}
WAKE = threading.Event()   # set on user actions: poll now, don't wait out the idle sleep

def _bump_action(which):
    ACTION_EPOCH[which] += 1
    STATE[which]["saw_up"] = False   # a fresh user action resets crash evidence
    WAKE.set()
    return ACTION_EPOCH[which]

CRASH_FAILS = 3      # consecutive failed samples before declaring crashed
LOG_STALE_S = 10     # log older than this (with health failing) counts as dead
POLL_FAST_S = 2      # while something is changing
POLL_IDLE_S = 5      # when both lanes are steady

def _log_info(p):
    try:
        st = p.stat()
        return st.st_size, st.st_mtime
    except Exception:
        return 0, 0

def _gpu_toks():
    for ln in reversed(_read_tail(GPU_LOG)):
        m = re.search(r"tg = (\d+\.\d+) t/s", ln)
        if m:
            return float(m.group(1))
    return None

def _gpu_ctx_from_log():
    """ctx the running llama-server was started with (from its load banner)."""
    for ln in _read_head(GPU_LOG):
        m = re.search(r"n_ctx_slot = (\d+)", ln)
        if m:
            return int(m.group(1))
    return None

_NPU_SCAN = {"offset": 0, "loaded": False}

def _npu_loaded():
    """Has the running geniex served a chat since it started? Scans only the
    bytes appended since the last poll; a shrunk log means a restart."""
    size, _ = _log_info(NPU_LOG)
    if size < _NPU_SCAN["offset"]:
        _NPU_SCAN.update(offset=0, loaded=False)
    if _NPU_SCAN["loaded"] or size == _NPU_SCAN["offset"]:
        _NPU_SCAN["offset"] = size
        return _NPU_SCAN["loaded"]
    try:
        with open(NPU_LOG, "rb") as f:
            f.seek(max(_NPU_SCAN["offset"], size - 4_000_000))
            chunk = f.read(size - f.tell()).decode("utf-8", errors="replace")
        if re.search(r"POST[^\n]*chat/completions", chunk):
            _NPU_SCAN["loaded"] = True
    except Exception:
        pass
    _NPU_SCAN["offset"] = size
    return _NPU_SCAN["loaded"]

def _free_ram_gb():
    try:
        out = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command",
             "[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)"],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())
    except Exception:
        return None

_GIN_RE = re.compile(
    r"\[GIN\] (\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2}) \| \d+ \| +([0-9.]+)([µmns]?)s +\| .* \| +(GET|POST) +\"([^\"]+)\"")

def _npu_busy(window=60):
    """NPU counts as 'doing work' if a chat completion OR a slow (>1.5 s) request
    landed within the window. The panel's own fast health-check GETs are ignored,
    so an idle server reads 'ready', not 'busy'."""
    now = time.time()
    for ln in reversed(_read_tail(NPU_LOG)[-200:]):
        m = _GIN_RE.search(ln)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y/%m/%d - %H:%M:%S"))
        except Exception:
            continue
        if now - ts > window:
            return False            # lines only get older from here
        dur = float(m.group(2)) / {"µ": 1e6, "u": 1e6, "m": 1e3, "n": 1e9}.get(m.group(3), 1)
        if "chat/completions" in m.group(5) or dur > 1.5:
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

# ---------------------------------------------------------------- RAM estimator
# VRAM (= shared system RAM on Adreno) needed by a GGUF at a given context:
#   weights + kv_cache + ~2.5 GB llama-server overhead.
# KV bytes/token parsed from the GGUF header: layers × kv_heads × head_dim,
# each fp16 (2 bytes) for K and V. llama-server uses a unified KV cache
# (kv_unified = true), so ctx is the total across slots — no per-slot factor.
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

# the ctx selected at the last GPU launch (for the UI's RAM hint and restart)
LAST_GPU = {"model": None, "ctx": None}

def _find_model_file(alias_or_file):
    """Model file for a served alias (qwen3-coder-30b -> qwen3-coder-30b-Q4_0.gguf)."""
    f = MODELS / alias_or_file
    if f.is_file():
        return f
    cands = sorted(x for x in MODELS.glob("*.gguf") if x.stem.startswith(alias_or_file))
    return cands[0] if cands else None

# ---------------------------------------------------------------- downloads
def download_state():
    """Every .gguf (and in-progress .gguf.part) with size + expected size."""
    exp = _load_expected()
    out = []
    for f in sorted(MODELS.glob("*.gguf")) + sorted(MODELS.glob("*.gguf.part")):
        try:
            size = f.stat().st_size
        except Exception:
            continue
        partial = f.suffix == ".part"
        name = f.name[:-5] if partial else f.name
        out.append({"name": name, "size": size, "expected": exp.get(name),
                    "partial": partial or bool(exp.get(name) and size < exp[name])})
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

def curl_cmd(url, out_path):
    return ["curl", "-L", "--fail", "-C", "-", "--retry", "60",
            "--retry-all-errors", "--retry-delay", "3",
            "--connect-timeout", "20",
            "-o", str(out_path), url]

# ---------------------------------------------------------------- poller
def _poller():
    last_ram = 0.0
    ram_free = None
    while True:
        now = time.time()
        # one tasklist call for both servers; only run the (slower) health
        # check when the process is alive — a dead port eats the full timeout
        procs = _procs_alive(["llama-server.exe", "geniex.exe"])
        gproc, nproc = procs["llama-server.exe"], procs["geniex.exe"]

        gup = _http_ok(f"http://{GATEWAY}:{GPU_PORT}/health", 3) if gproc is not False else False
        gsize, gmtime = _log_info(GPU_LOG)
        gmodel = gpu_loaded_model() if gup else None
        gtoks = _gpu_toks() if gup else None
        # adopt the running model's ctx from its log (server prints it at load)
        if gup and gmodel and LAST_GPU["model"] is None:
            f = _find_model_file(gmodel)
            LAST_GPU["model"] = f.name if f else None
            LAST_GPU["ctx"] = _gpu_ctx_from_log() or 16384
        gbusy = gup and (now - gmtime) < 3   # llama log grows only during inference
        gstate, gsince, gdetail = _derive("gpu", gup, gbusy, gmtime, now, gproc)

        nup = _http_ok(f"http://{GATEWAY}:{NPU_PORT}/v1/models", 3) if nproc is not False else False
        nsize, nmtime = _log_info(NPU_LOG)
        nloaded = _npu_loaded() if nup else False
        nbusy = nup and _npu_busy()
        nstate, nsince, ndetail = _derive("npu", nup, nbusy, nmtime, now, nproc)

        wup = _http_ok(f"http://127.0.0.1:{WEBUI_PORT}/health", 3)

        if now - last_ram > 30:
            last_ram = now
            ram_free = _free_ram_gb() or ram_free

        gpu_ram = _gpu_ram_need(LAST_GPU["model"], LAST_GPU["ctx"]) if LAST_GPU["model"] else None

        with CACHE_LOCK:
            CACHE.update({
                "gpu": {"up": gup, "model": gmodel, "toks": gtoks,
                        "state": gstate, "since": gsince, "detail": gdetail,
                        "log_tail": _tail(GPU_LOG, 8)},
                "npu": {"up": nup, "loaded": nloaded,
                        "state": nstate, "since": nsince, "detail": ndetail,
                        "log_tail": _tail(NPU_LOG, 8)},
                "webui": wup,
                "ram_free": ram_free,
                "gpu_ram": gpu_ram,
                "models": download_state(),
                "now": time.strftime("%H:%M:%S"),
            })

        # poll slower when nothing is changing: fewer Windows process launches
        steady = (INTENT["gpu"] is None and INTENT["npu"] is None
                  and gstate in ("ready", "off") and nstate in ("ready", "off"))
        WAKE.wait(POLL_IDLE_S if steady else POLL_FAST_S)
        WAKE.clear()

# ---------------------------------------------------------------- quantize
QUANTS = ["Q4_0", "Q4_1", "Q5_0", "Q5_1", "Q8_0", "Q3_K_M", "Q4_K_M", "Q5_K_M", "Q6_K", "IQ4_XS"]

# ---------------------------------------------------------------- FastAPI app
import httpx
from fastapi import WebSocket
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask
import websockets

WEBUI_BASE = f"http://127.0.0.1:{WEBUI_PORT}"
HTTP = None   # shared httpx client for the webui proxy (created in lifespan)

@asynccontextmanager
async def lifespan(app):
    global HTTP
    HTTP = httpx.AsyncClient(base_url=WEBUI_BASE,
                             timeout=httpx.Timeout(600, connect=5, pool=30))
    threading.Thread(target=_poller, daemon=True).start()
    yield
    await HTTP.aclose()

app = FastAPI(title="llmnpu Panel", lifespan=lifespan)
api = APIRouter(prefix="/api", dependencies=[Depends(guard_host)])
write = [Depends(guard_write)]

# NOTE: "/" deliberately has NO panel route — the catch-all at the bottom proxies
# it to Open WebUI, which is what the <iframe src="/"> in /panel needs.
# Only the paths below are panel routes; every other /api/* path (Open WebUI's
# own API) falls through to the proxy.

# ---- status
@api.get("/status")
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
                "ctx": LAST_GPU["ctx"], "log_tail": g.get("log_tail", [])},
        "npu": {"up": n.get("up", False), "loaded": n.get("loaded", False),
                "state": n.get("state", "off"), "since": n.get("since", 0),
                "detail": n.get("detail", ""), "log_tail": n.get("log_tail", [])},
        "webui": c.get("webui", False),
        "ram_free": c.get("ram_free"),
        "gpu_ram": c.get("gpu_ram"),
        "gateway": GATEWAY,
        "models": c.get("models", []),
        "jobs": jobs,
        "now": c.get("now", time.strftime("%H:%M:%S")),
    }

# ---- ctx RAM estimate: given model + ctx, how much RAM will it need?
@api.get("/ctx/estimate")
def ctx_estimate(model: str, ctx: int):
    f = _model_path(model)
    ctx = _safe_ctx(ctx)
    need = _gpu_ram_need(f.name, ctx)
    return {"model": f.name, "ctx": ctx, "need_gb": None if need is None else round(need, 1)}

# ---- GPU lifecycle
@api.post("/gpu/start", dependencies=write)
def gpu_start(model: str, ctx: int = 16384, alias: str = ""):
    f = _model_path(model)
    _check_complete(f)   # never load a half-written .gguf
    ctx = _safe_ctx(ctx)
    alias = _safe_alias(alias, f.name)
    INTENT["gpu"] = {"action": "start", "model": f.name, "since": time.time()}
    LAST_GPU["model"], LAST_GPU["ctx"] = f.name, ctx
    start_gpu(f.name, ctx, alias, _bump_action("gpu"))
    return {"ok": True, "model": f.name, "alias": alias, "ctx": ctx}

@api.post("/gpu/stop", dependencies=write)
def gpu_stop():
    INTENT["gpu"] = {"action": "stop", "since": time.time()}
    _bump_action("gpu")
    _taskkill_async("llama-server.exe")
    return {"ok": True}

@api.post("/gpu/restart", dependencies=write)
def gpu_restart(model: str = "", ctx: int | None = None, alias: str = ""):
    """Restart the given model, or the loaded one. ctx defaults to the ctx of
    the last launch, so a restart never silently shrinks the window."""
    if model:
        f = _model_path(model)
    else:
        loaded = gpu_loaded_model() or LAST_GPU["model"]
        f = _find_model_file(loaded) if loaded else None
        if not f:
            raise HTTPException(400, "no model loaded and none given")
        f = _model_path(f.name)
    _check_complete(f)
    ctx = _safe_ctx(ctx if ctx is not None else (LAST_GPU["ctx"] or 16384))
    alias = _safe_alias(alias, f.name)
    INTENT["gpu"] = {"action": "restart", "model": f.name, "since": time.time()}
    LAST_GPU["model"], LAST_GPU["ctx"] = f.name, ctx
    start_gpu(f.name, ctx, alias, _bump_action("gpu"))
    return {"ok": True, "model": f.name, "alias": alias, "ctx": ctx}

@api.post("/models/delete", dependencies=write)
def model_delete(model: str):
    f = _model_path(model)
    name, stem = f.name, f.stem
    # the served alias often differs from the filename (alias qwen3-coder-30b
    # vs file qwen3-coder-30b-Q4_0.gguf) — match on either direction so the
    # loaded model can never be deleted out from under the server
    if gpu_up():
        loaded = gpu_loaded_model()
        if not loaded:
            raise HTTPException(409, "GPU server is up but loaded model is unknown — stop it first")
        if (loaded in (name, stem) or stem.startswith(loaded)
                or name.startswith(loaded) or loaded.startswith(stem)):
            raise HTTPException(409, "model is currently loaded — stop the GPU server first")
    try:
        f.unlink()
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    _save_expected(name, None)
    return {"ok": True, "deleted": name}

@api.post("/npu/start", dependencies=write)
def npu_start():
    INTENT["npu"] = {"action": "start", "since": time.time()}
    _bump_action("npu")
    if not npu_up():
        threading.Thread(target=_launch_npu, daemon=True).start()
    return {"ok": True}

@api.post("/npu/stop", dependencies=write)
def npu_stop():
    INTENT["npu"] = {"action": "stop", "since": time.time()}
    _bump_action("npu")
    _taskkill_async("geniex.exe")
    return {"ok": True}

@api.post("/npu/restart", dependencies=write)
def npu_restart():
    # unconditional kill-then-start: works whether the server is healthy,
    # half-dead, or already gone (the toggle's stale-state trap)
    INTENT["npu"] = {"action": "restart", "since": time.time()}
    my_epoch = _bump_action("npu")
    def _restart():
        _down_wait("geniex.exe")
        if ACTION_EPOCH["npu"] != my_epoch:
            return   # a newer start/stop/restart superseded this restart
        _launch_npu()
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True}

# ---- logs
@api.get("/logs/{which}")
def logs(which: str, n: int = 50):
    p = {"gpu": GPU_LOG, "npu": NPU_LOG}.get(which)
    if not p:
        raise HTTPException(404, "unknown log")
    return {"log": _read_tail(p, 1_000_000)[-max(1, min(n, 2000)):]}

# ---- downloads
@api.post("/download", dependencies=write)
def download(url: str, name: str = ""):
    url = url.strip()
    if not re.match(r"^https?://[^\s\"']+$", url) or ".gguf" not in url.lower():
        raise HTTPException(400, "must be a direct http(s) URL to a .gguf file")
    fname = _safe_model_name(name.strip() or url.split("?")[0].rstrip("/").split("/")[-1])
    final = MODELS / fname
    if final.exists():
        raise HTTPException(409, f"{fname} already exists")
    if _job_running("download"):
        raise HTTPException(409, "a download is already running")
    part = MODELS / (fname + ".part")
    size = hf_size(url)
    if size:
        _save_expected(fname, size)

    def _finish():
        got = part.stat().st_size
        if size and got != size:
            return f"size mismatch {got}/{size} — kept {part.name}"
        part.rename(final)
        return f"saved {fname}"

    # curl writes to .part and resumes it (-C -); renamed only when complete,
    # so a half-downloaded file is never offered as a loadable model
    job = spawn_job("download", f"⤓ {fname}", curl_cmd(url, part), on_done=_finish)
    return {"ok": True, "job": job.id, "file": fname, "size": size}

# ---- quantize
@api.get("/quantize/targets")
def quant_targets():
    return QUANTS

@api.post("/quantize", dependencies=write)
def quantize(model: str, quant: str):
    src = _model_path(model)
    if quant not in QUANTS:
        raise HTTPException(400, f"unknown quant {quant}")
    out = MODELS / f"{src.stem}-{quant}.gguf"
    if _job_running("quantize"):
        raise HTTPException(409, "a quantize job is already running")
    job = spawn_job("quantize", f"⚙ {out.name}", [str(QUANTIZER), to_win(src), to_win(out), quant])
    return {"ok": True, "job": job.id, "out": out.name}

# ---- NPU pull
@api.post("/npu/pull", dependencies=write)
def npu_pull(repo: str):
    repo = repo.strip()
    if not re.match(r"^[\w.\-]+/[\w.\-]+(:\w+)?$", repo):
        raise HTTPException(400, "bad model id, expected org/name[:precision]")
    if _job_running("npull"):
        raise HTTPException(409, "an NPU pull is already running")
    cmd = [CMD, "/c", to_win(GENIEX),
           "--data-dir", to_win(WIN_ROOT / "geniex" / "models" / "geniex"),
           "--skip-update", "pull", repo]
    job = spawn_job("npull", f"NPU ⤓ {repo}", cmd)
    return {"ok": True, "job": job.id}

# ---- job control
@api.get("/jobs")
def jobs():
    with JOBS_LOCK:
        return [j.to_dict() for j in JOBS.values()]

# Killing the WSL-side wrapper (cmd.exe / interop proxy) can leave the Windows
# process running, so cancel also kills the Windows process itself. The NPU
# pull shares geniex.exe with the NPU server: match its command line only.
_CANCEL_WIN = {
    "quantize": [CMD, "/c", "taskkill /F /T /IM llama-quantize.exe"],
    "npull": [POWERSHELL, "-NoProfile", "-Command",
              "Get-CimInstance Win32_Process -Filter \"Name='geniex.exe'\" | "
              "Where-Object { $_.CommandLine -match ' pull ' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
}

@api.post("/jobs/{jid}/cancel", dependencies=write)
def job_cancel(jid: str):
    with JOBS_LOCK:
        j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    if j.proc and j.proc.poll() is None:
        j.cancelled = True
        try:
            j.proc.terminate()
        except Exception:
            pass
        if j.kind in _CANCEL_WIN:
            try:
                subprocess.run(_CANCEL_WIN[j.kind], capture_output=True, timeout=30)
            except Exception:
                pass
    return {"ok": True}

app.include_router(api)

# ---- Open WebUI embedded at the ROOT of :8188 (its absolute paths work untouched).
# Panel's own routes (/panel, /api/*) are registered above and take precedence.
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
    import asyncio
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

async def _proxy(request: Request):
    url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP | {"accept-encoding"}}
    body = await request.body()
    upstream = None
    try:
        req = HTTP.build_request(request.method, url, headers=headers, content=body)
        upstream = await HTTP.send(req, stream=True)
        enc = upstream.headers.get("content-encoding", "").lower()
        if enc:  # httpx aread() auto-decompresses; strip the stale header
            raw = await upstream.aread()
            await upstream.aclose()
            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in HOP | {"content-encoding", "content-length", "vary", "etag"}}
            return Response(content=raw, status_code=upstream.status_code,
                            headers=out_headers)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        # close the upstream response once the body is streamed (or the client
        # goes away) so pooled connections are returned, never leaked
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=out_headers, background=BackgroundTask(upstream.aclose))
    except Exception:
        if upstream is not None:
            await upstream.aclose()
        return JSONResponse({"detail": "Open WebUI not reachable on :3000"}, status_code=502)

# catch-all: everything not matched above goes to the webui
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"])
async def webui_proxy(path: str, request: Request):
    return await _proxy(request)

if __name__ == "__main__":
    print(f"llmnpu Panel on http://localhost:{PORT}/panel  (gateway {GATEWAY}, windows root {WIN_ROOT})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
