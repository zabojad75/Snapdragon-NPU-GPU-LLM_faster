#!/usr/bin/env python3
"""Map-reduce context pipeline across the llmnpu GPU + NPU lanes.

Processes inputs too big to fit one server's context window by chunking them,
mapping each chunk to an extraction prompt (routed across lanes in parallel),
then reducing the summaries into a final answer.

Lane strengths (from docs/BENCHMARKS.md):
  - NPU  (geniex :18181): prefill ~1000 t/s, ctx HARD-LOCKED at 4096 -> small chunks
  - GPU  (llama-server :8081): prefill 261-400 t/s, ctx read from the server's
    /props (whatever the panel started it with) -> bigger chunks + final reduce. When the Adreno driver is broken the same server runs on
    CPU fallback - still usable, just slower, no special-casing needed.

Routing is capacity-aware: NPU takes chunks that fit its 4K budget, GPU gets
the rest (packing several small chunks per call); if only one lane is up,
everything runs there with budgets adjusted. A "prompt too long" reply from
the NPU requeues that chunk for the GPU phase (never GPU+NPU decode at once).

Usage:
  python3 scripts/context_pipeline.py --input FILE_OR_GLOB --question "task" \
      [--out result.md] [--map-lanes npu,gpu] [--chunk-tokens 3000] \
      [--npu-model auto] [--gpu-model auto] [--reduce-ctx N] \
      [--dry-run] [--verbose]

Works from WSL (gateway IP) or Windows (localhost) - same logic as
scripts/qwen_gen.py. Token counts are estimated from a chars-per-token ratio
calibrated once against the GPU server's /tokenize (chars/3.2 if it's down).
"""
import argparse
import concurrent.futures as cf
import glob as globlib
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# line-buffer stdout so progress is visible under nohup/redirect
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

GPU_PORT = 8081
NPU_PORT = 18181
NPU_CTX = 4096            # hard-locked by GenieX (static shapes)
GPU_CTX_DEFAULT = 16384   # used only if the server's /props can't be read

# Bump when the map prompt changes: checkpointed summaries made with an older
# prompt (or another model) must not be reused.
MAP_PROMPT_VERSION = 2

# chars per token: conservative default (code and French text run ~3-3.5,
# English prose ~4); replaced by calibrate_tokens() when the GPU lane is up
CHARS_PER_TOKEN = 3.2

# Map-prompt overhead: instructions + question + answer headroom, in tokens.
MAP_OVERHEAD_TOKENS = 350
# Reduce-phase overhead: question + instructions + answer headroom.
REDUCE_OVERHEAD_TOKENS = 1200

# NPU RAM gotcha (QUICKREF): 30B GPU (~19 GB) + Gemma-4 NPU (~9 GB) risks OOM
# on 48 GB. Auto-pick avoids Gemma-4 when a 30B is served on the GPU.
UNSAFE_NPU_WITH_30B = ("Gemma-4-E4B", "gemma_4_e4b")


def gateway():
    """WSL NAT gateway IP (Windows host). Falls back to localhost on Windows."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return "localhost"
    parts = out.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    for tok in reversed(parts):
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", tok):
            return tok
    return "localhost"


def http_json(url, body, timeout=1800):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        # read the body once and keep it: callers inspect it after the fact
        try:
            e.body_text = e.read().decode(errors="replace")
        except Exception:
            e.body_text = ""
        raise
    d = json.loads(raw)
    d["_dt"] = time.time() - t0
    return d


def get_json(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def get_models(host, port):
    """Return list of model ids from /v1/models, or [] if lane is down."""
    d = get_json(f"http://{host}:{port}/v1/models")
    return [m.get("id", "") for m in d.get("data", [])] if d else []


def gpu_server_ctx(host):
    """Per-request context of the running llama-server (n_ctx of a slot;
    with the unified KV cache that is the whole window), or None."""
    d = get_json(f"http://{host}:{GPU_PORT}/props")
    try:
        return int(d["default_generation_settings"]["n_ctx"])
    except Exception:
        return None


def calibrate_tokens(host, inputs, sample_chars=120_000):
    """Measure chars/token on a sample of the input with the GPU server's
    tokenizer. Keeps the conservative default if the server is unreachable."""
    global CHARS_PER_TOKEN
    sample = "".join(t for _, t in inputs)[:sample_chars]
    if not sample:
        return
    try:
        d = http_json(f"http://{host}:{GPU_PORT}/tokenize",
                      {"content": sample}, timeout=60)
        n = len(d.get("tokens", []))
    except Exception:
        return
    if n:
        # 5% margin: the NPU model's tokenizer can differ slightly
        CHARS_PER_TOKEN = len(sample) / n / 1.05


# ---------------------------------------------------------------- chunking

def est_tokens(text):
    """Token estimate from the calibrated chars-per-token ratio."""
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def collect_inputs(spec):
    """Expand --input into an ordered list of (path, text)."""
    files = []
    for item in spec:
        if os.path.isdir(item):
            for root, dirs, fnames in os.walk(item):
                dirs[:] = sorted(d for d in dirs
                                 if d not in (".git", "__pycache__",
                                              "node_modules", "build"))
                for fn in sorted(fnames):
                    files.append(os.path.join(root, fn))
        elif os.path.isfile(item):
            files.append(item)
        else:
            files.extend(sorted(globlib.glob(item, recursive=True)))
    if not files:
        sys.exit(f"error: no input files matched {spec}")
    seen, out = set(), []
    for f in dict.fromkeys(files):        # dedupe, keep order
        if f in seen:
            continue
        seen.add(f)
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            print(f"warn: cannot read {f}: {e}", file=sys.stderr)
            continue
        if b"\x00" in raw[:8192]:
            print(f"skip: binary file {f}", file=sys.stderr)
            continue
        out.append((f, raw.decode("utf-8", errors="replace")))
    if not out:
        sys.exit("error: all matched inputs unreadable")
    return out


FENCE_RE = re.compile(r"^\s*(```|~~~)")


def split_fences(text):
    """Split text into (unclosed_fence, block) pieces on ``` / ~~~ fences.

    Pieces are cut-safe boundaries: a piece either contains no fence or a
    complete (open+close) fence. An unclosed fence at EOF keeps its True flag
    so chunk_text knows never to cut around it.
    """
    pieces, buf, in_fence, fence_open = [], [], False, ""
    for line in text.splitlines(keepends=True):
        m = FENCE_RE.match(line)
        if m and not in_fence:
            if buf:                       # flush preceding prose
                pieces.append((False, "".join(buf)))
                buf = []
            in_fence, fence_open = True, m.group(1)
            buf.append(line)
        elif m and in_fence and m.group(1).startswith(fence_open):
            in_fence, fence_open = False, ""
            buf.append(line)
            pieces.append((False, "".join(buf)))   # complete fence block
            buf = []
        else:
            buf.append(line)
    if buf:
        pieces.append((in_fence, "".join(buf)))
    return pieces


def _split_oversize(block, max_tokens):
    """Hard-split an oversize block by lines, <= max_tokens each."""
    out, buf, size = [], [], 0
    for line in block.splitlines(keepends=True):
        lt = est_tokens(line)
        if buf and size + lt > max_tokens:
            out.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += lt
    if buf:
        out.append("".join(buf))
    return out


def chunk_text(text, max_tokens):
    """Split text into chunks <= max_tokens, fence- and paragraph-aware.

    Cut-safe pieces (prose, complete fences) are packed together up to the
    budget; an oversize piece is hard-split by lines — for an oversize code
    fence this cuts inside the fence, which is unavoidable and marked as a
    continuation by the chunk headers.
    """
    chunks, buf, size = [], [], 0

    def flush():
        nonlocal buf, size
        if buf:
            chunks.append("".join(buf))
        buf, size = [], 0

    for _, piece in split_fences(text):
        pt = est_tokens(piece)
        if pt > max_tokens:
            flush()
            chunks.extend(_split_oversize(piece, max_tokens))
            continue
        if buf and size + pt > max_tokens:
            flush()
        buf.append(piece)
        size += pt
    flush()
    return [c for c in chunks if c.strip()]


def make_chunks(inputs, chunk_tokens):
    """Return list of chunk dicts with provenance headers."""
    chunks = []
    for path, text in inputs:
        parts = chunk_text(text, chunk_tokens)
        total = len(parts)
        for i, part in enumerate(parts):
            header = f"[CHUNK {i + 1}/{total} of file {path}]\n"
            chunks.append({
                "id": f"{os.path.basename(path)}#{i + 1}",
                "path": path,
                "tokens": est_tokens(part),
                "text": header + part,
            })
    return chunks


# ---------------------------------------------------------------- lanes

class Lane:
    def __init__(self, name, host, port, model, ctx, concurrent):
        self.name, self.host, self.port = name, host, port
        self.model, self.ctx, self.concurrent = model, ctx, concurrent
        self.lock = threading.Lock()
        self.calls = 0
        self.prompt_toks = 0
        self.completion_toks = 0
        self.wall = 0.0

    @property
    def base(self):
        return f"http://{self.host}:{self.port}/v1"

    RETRY_DELAYS = (2, 5, 10)

    def chat(self, messages, max_tokens, temperature):
        body = {"model": self.model, "messages": messages,
                "max_tokens": max_tokens, "temperature": temperature}
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                d = http_json(f"{self.base}/chat/completions", body)
                break
            except Exception as e:
                if attempt == len(self.RETRY_DELAYS) or not is_transient(e):
                    raise
                delay = self.RETRY_DELAYS[attempt]
                print(f"  {self.name}: transient error ({e}), retry in {delay}s",
                      file=sys.stderr)
                time.sleep(delay)
        with self.lock:
            self.calls += 1
            u = d.get("usage", {})
            self.prompt_toks += u.get("prompt_tokens", 0)
            self.completion_toks += u.get("completion_tokens", 0)
            self.wall += d.get("_dt", 0.0)
        msg = d["choices"][0]["message"]
        # reasoning_content (gpt-oss style) is deliberately dropped: it is the
        # model thinking aloud, not notes/answer, and would pollute the reduce
        content = msg.get("content") or ""
        # some models/backends inline <think>...</think> in content - strip it
        if "</think>" in content:
            content = content.split("</think>", 1)[1].lstrip()
        return content

    def usable_tokens(self, overhead=MAP_OVERHEAD_TOKENS):
        return max(512, self.ctx - overhead)


def discover_lanes(args, host):
    """Probe both servers; build the active Lane list per --map-lanes."""
    gpu_models = get_models(host, GPU_PORT)
    npu_models = get_models(host, NPU_PORT)
    want = [s.strip().lower() for s in args.map_lanes.split(",") if s.strip()]
    lanes = []

    # ---- GPU lane ("cpu" alias: llama-server on CPU fallback = same server)
    if ("gpu" in want or "cpu" in want) and gpu_models:
        if args.gpu_model == "auto":
            pref = [m for m in gpu_models if "30b" in m.lower()]
            model = pref[0] if pref else gpu_models[0]
        else:
            model = args.gpu_model
            if model not in gpu_models:
                sys.exit(f"error: --gpu-model {model} not on GPU server "
                         f"(served: {gpu_models})")
        # ctx: explicit --reduce-ctx, else what the server actually runs with.
        # Concurrency 1: the KV cache is unified (one window shared by all
        # slots), so two near-full packed calls can't fit side by side — and
        # parallel prefill on one GPU gains little anyway.
        ctx = args.reduce_ctx or gpu_server_ctx(host) or GPU_CTX_DEFAULT
        lanes.append(Lane("gpu", host, GPU_PORT, model, ctx, 1))

    # ---- NPU lane
    if "npu" in want and npu_models:
        loaded_30b = any("30b" in m.lower() for m in gpu_models)
        if args.npu_model == "auto":
            safe = [m for m in npu_models
                    if not any(b in m for b in UNSAFE_NPU_WITH_30B)]
            if loaded_30b and not safe:
                print("warn: 30B on GPU + only Gemma-4 on NPU - RAM-unsafe "
                      "combo, NPU lane disabled (override with --npu-model)",
                      file=sys.stderr)
            else:
                pool = safe if loaded_30b else npu_models
                # map workhorse preference: 8B first (strong extraction),
                # 0.6B only if nothing bigger is available
                for tag in ("8B", "0.6B"):
                    pref = [m for m in pool if tag in m]
                    if pref:
                        lanes.append(Lane("npu", host, NPU_PORT, pref[0],
                                           NPU_CTX, 1))
                        break
        else:
            if args.npu_model not in npu_models:
                sys.exit(f"error: --npu-model {args.npu_model} not on NPU "
                         f"server (served: {npu_models})")
            if loaded_30b and any(b in args.npu_model for b in UNSAFE_NPU_WITH_30B):
                print("warn: 30B GPU + Gemma-4 NPU is the known OOM combo "
                      "(QUICKREF) - proceeding anyway, RAM risk is yours",
                      file=sys.stderr)
            lanes.append(Lane("npu", host, NPU_PORT, args.npu_model, NPU_CTX, 1))

    if not lanes:
        up = ([f"GPU :{GPU_PORT} {gpu_models}"] if gpu_models else []) + \
             ([f"NPU :{NPU_PORT} {npu_models}"] if npu_models else [])
        sys.exit("error: no lanes reachable - start servers via the panel\n"
                 f"  reachable: {up or 'none'}")
    return lanes


# ---------------------------------------------------------------- checkpoint
# The SoC has bugchecked under sustained NPU load (docs/QUICKREF.md).
# Map results are therefore checkpointed to disk as they complete; a rerun
# after a crash reuses every finished chunk instead of redoing everything.

def checkpoint_path(out_file):
    return out_file + ".ckpt.jsonl"


def chunk_key(model, question, text):
    """Stable per-chunk key: model + map prompt version + question + chunk.
    A different model or prompt must not reuse old summaries."""
    h = hashlib.sha256()
    h.update(f"{model}\x00v{MAP_PROMPT_VERSION}\x00".encode())
    h.update(question.encode())
    h.update(b"\x00")
    h.update(text.encode())
    return h.hexdigest()[:32]


def load_checkpoint(path):
    """Return {key: summary} from a checkpoint file."""
    done = {}
    if not os.path.exists(path):
        return done
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    done[rec["k"]] = rec["s"]
                except (json.JSONDecodeError, KeyError):
                    continue   # tolerate a torn final line after a crash
    except OSError:
        return {}
    return done


class CheckpointWriter:
    """Append-only JSONL writer, crash-safe per line."""

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "a", encoding="utf-8")

    def add(self, key, summary):
        self.fh.write(json.dumps({"k": key, "s": summary},
                                 ensure_ascii=False) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------- map phase

def map_messages(question, chunk_text):
    return [
        {"role": "system",
         "content": "You are a precise technical analyst. Extract and preserve "
                    "facts relevant to the user's task. Keep identifiers, "
                    "code snippets, exact numbers, paths and names verbatim. "
                    "Do NOT show your reasoning or chain of thought. "
                    "Output ONLY compact bullet notes, at most 250 words."},
        {"role": "user",
         "content": f"TASK: {question}\n\n"
                    "Below is one chunk of a larger context. Extract "
                    "EVERYTHING relevant to the task; ignore the rest. "
                    "Output only the notes, no preamble.\n\n" + chunk_text},
    ]


def is_too_long_error(e):
    # geniex answers 400 or 500 (SDKError), llama-server 400
    if not isinstance(e, urllib.error.HTTPError) or e.code not in (400, 413, 500):
        return False
    body = getattr(e, "body_text", "")
    low = ((str(getattr(e, "reason", "")) or "") + " " + body).lower()
    return ("too long" in low or "exceed" in low
            or ("context" in low and ("size" in low or "length" in low)))


def is_transient(e):
    """Worth retrying: connection trouble or an overloaded server — never a
    request the server rejected (4xx) or a context overflow."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (429, 502, 503, 504) and not is_too_long_error(e)
    return isinstance(e, (urllib.error.URLError, ConnectionError,
                          http.client.RemoteDisconnected,
                          http.client.IncompleteRead))


class TooLong(Exception):
    """The lane refused the chunk as too long: requeue it for a wider lane."""


def map_chunk(lane, chunk_text, question, max_tokens, temp):
    try:
        return lane.chat(map_messages(question, chunk_text), max_tokens, temp)
    except urllib.error.HTTPError as e:
        if is_too_long_error(e):
            raise TooLong(str(e)) from e
        raise


def pack_small_chunks(chunks, budget):
    """Greedily pack chunks into calls <= budget tokens (for wide lanes)."""
    calls, cur, size = [], [], 0
    for c in chunks:
        ct = c["tokens"] + 40     # header overhead per chunk
        if cur and size + ct > budget:
            calls.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += ct
    if cur:
        calls.append(cur)
    return calls


# ---------------------------------------------------------------- reduce phase

def reduce_messages(question, notes, final):
    if final:
        instr = ("You are given notes extracted from many chunks of a large "
                 "context. Use them to answer the user's task fully. Preserve "
                 "exact identifiers, numbers, paths and code. Cite source "
                 "files when the notes mention them.")
    else:
        instr = ("You are given notes extracted from many chunks of a large "
                 "context. Merge them: deduplicate, resolve contradictions, "
                 "keep every fact, identifier, number and code snippet.")
    return [
        {"role": "system", "content": "You are an expert technical assistant."},
        {"role": "user",
         "content": f"TASK: {question}\n\n{instr}\n\n"
                    "=== EXTRACTED NOTES ===\n" + "\n\n".join(notes)},
    ]


def hierarchical_reduce(reducer, question, notes, temp,
                        max_tokens, final_max_tokens, verbose=False):
    """Merge notes in batches until one fits, then answer."""
    budget = reducer.usable_tokens(REDUCE_OVERHEAD_TOKENS)

    def pack(items, sep_extra=20):
        batches, cur, size = [], [], 0
        for it in items:
            t = est_tokens(it) + sep_extra
            if cur and size + t > budget:
                batches.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += t
        if cur:
            batches.append(cur)
        return batches

    level = 0
    while True:
        batches = pack(notes)
        if len(batches) <= 1:
            break
        level += 1
        print(f"reduce level {level}: merging {len(batches)} note batch(es)")
        merged = [None] * len(batches)
        with cf.ThreadPoolExecutor(max_workers=reducer.concurrent) as ex:
            futs = {ex.submit(reducer.chat,
                              reduce_messages(question, b, final=False),
                              max_tokens, temp): i
                    for i, b in enumerate(batches)}
            for f in cf.as_completed(futs):
                merged[futs[f]] = f.result()
        notes = [m for m in merged if m]

    print("final reduce: answering from merged notes")
    return reducer.chat(reduce_messages(question, notes, final=True),
                         final_max_tokens, temp)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Map-reduce a big context across GPU+NPU lanes.",
        epilog="examples:\n"
               "  context_pipeline.py --input docs/ --out r.md -q 'summarize the architecture'\n"
               "  context_pipeline.py --input src/**/*.py -q 'find all API endpoints' --dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", nargs="+", required=True,
                    help="file, dir, or glob (repeatable)")
    ap.add_argument("--question", "-q", required=True,
                    help="what to extract / answer from the context")
    ap.add_argument("--out", default="pipeline_result.md",
                    help="output file for the final answer")
    ap.add_argument("--manifest", default=None,
                    help="JSON manifest path (default: <out>.manifest.json)")
    ap.add_argument("--map-lanes", default="npu,gpu",
                    help="comma list of lanes: npu,gpu (default npu,gpu)")
    ap.add_argument("--chunk-tokens", type=int, default=3000,
                    help="max tokens per chunk (default 3000, NPU-safe)")
    ap.add_argument("--gpu-model", default="auto",
                    help="GPU model id (default: auto-pick 30B if served)")
    ap.add_argument("--npu-model", default="auto",
                    help="NPU model id (default: RAM-safe auto-pick)")
    ap.add_argument("--reduce-ctx", type=int, default=None,
                    help="GPU context budget per call (default: read from the "
                    "running llama-server)")
    ap.add_argument("--map-max-tokens", type=int, default=500,
                    help="max generated tokens per map call")
    ap.add_argument("--final-max-tokens", type=int, default=2000,
                    help="max generated tokens for the final answer")
    ap.add_argument("--temp", type=float, default=0.2)
    ap.add_argument("--npu-cooldown", type=float, default=2.0,
                    help="seconds of rest between NPU map calls (default 2). "
                    "This SoC has bugchecked (0x18b) under sustained NPU+GPU "
                    "load - see docs/QUICKREF.md; raise to 5+ for extra margin")
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore existing checkpoint, map everything fresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="chunk + plan routing, print stats, no LLM calls")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    host = gateway()
    inputs = collect_inputs(a.input)
    calibrate_tokens(host, inputs)
    total_in_toks = sum(est_tokens(t) for _, t in inputs)
    chunks = make_chunks(inputs, a.chunk_tokens)
    print(f"input: {len(inputs)} files, ~{total_in_toks:,} tokens "
          f"({CHARS_PER_TOKEN:.2f} chars/token) "
          f"-> {len(chunks)} chunks (target <= {a.chunk_tokens} t)")

    lanes = discover_lanes(a, host)
    npu = next((l for l in lanes if l.name == "npu"), None)
    gpu = next((l for l in lanes if l.name == "gpu"), None)
    for lane in lanes:
        print(f"lane {lane.name}: model={lane.model} ctx={lane.ctx} "
              f"budget/call={lane.usable_tokens()} t")

    # ---- route: NPU takes what fits its 4K budget, GPU packs the rest.
    # Chunks routed to a lane are re-split if they exceed that lane's
    # per-call budget (e.g. --chunk-tokens 6000 vs --reduce-ctx 4096).
    def resplit(chunks_list, budget):
        out = []
        for c in chunks_list:
            if c["tokens"] <= budget:
                out.append(c)
            else:
                for j, p in enumerate(chunk_text(c["text"], budget), 1):
                    out.append({"id": f"{c['id']}.{j}", "path": c["path"],
                                "tokens": est_tokens(p), "text": p})
        return out

    if npu and gpu:
        npu_queue = resplit([c for c in chunks
                             if c["tokens"] <= npu.usable_tokens()],
                            npu.usable_tokens())
        gpu_rest = resplit([c for c in chunks
                            if c["tokens"] > npu.usable_tokens()],
                           gpu.usable_tokens())
        gpu_calls = pack_small_chunks(gpu_rest, gpu.usable_tokens())
    elif gpu:
        npu_queue = []
        gpu_calls = pack_small_chunks(resplit(chunks, gpu.usable_tokens()),
                                      gpu.usable_tokens())
    else:   # NPU only: everything must fit 4096
        npu_queue = resplit(chunks, npu.usable_tokens())
        gpu_calls = []

    n_npu = len(npu_queue)
    n_gpu_chunks = sum(len(call) for call in gpu_calls)
    print(f"plan: {n_npu} chunk(s) -> NPU, {n_gpu_chunks} chunk(s) in "
          f"{len(gpu_calls)} packed call(s) -> GPU")

    if a.dry_run:
        for path in sorted({c["path"] for c in chunks}):
            n = sum(1 for c in chunks if c["path"] == path)
            print(f"  {path}: {n} chunk(s)")
        print("dry-run complete, no LLM calls made")
        return

    t_start = time.time()
    summaries = {}       # plan position -> summary (stable order for reduce)

    def run_npu(c):
        time.sleep(a.npu_cooldown)   # rest between sustained NPU calls: this
                                     # SoC has bugchecked (0x18b) under long
                                     # NPU+GPU load - see docs/QUICKREF.md
        return map_chunk(npu, c["text"], a.question, a.map_max_tokens, a.temp)

    def run_gpu(call):
        text = "\n\n".join(c["text"] for c in call)
        return map_chunk(gpu, text, a.question, a.map_max_tokens, a.temp)

    # Phased map schedule: NPU queue first, then GPU packed calls - never both
    # decoding at once. On this SoC, concurrent GPU+NPU decode makes both
    # collapse (~2 t/s measured vs 17-25 solo): prefill(NPU)+decode(GPU) is
    # fine, but decode+decode saturates the shared DRAM/power budget.
    t_map = time.time()
    errors = [0]
    too_long = []          # (position, chunk) the NPU refused -> GPU phase

    # checkpoint: summaries survive a crash; rerun reuses them
    ckpt_file = checkpoint_path(a.out)
    if a.no_resume and os.path.exists(ckpt_file):
        os.remove(ckpt_file)
    done_keys = load_checkpoint(ckpt_file)
    writer = CheckpointWriter(ckpt_file)
    reused = 0

    def drain(executor, jobs, lane_name):
        """Run jobs (position, zero-arg callable, tag, key, chunk), print progress."""
        futs = {}
        for pos, fn, tag, key, chunk in jobs:
            futs[executor.submit(fn)] = (lane_name, pos, tag, key, chunk)
        done = 0
        for fut in cf.as_completed(futs):
            lane_name_, position, tag, key, chunk = futs[fut]
            done += 1
            try:
                summary = fut.result()
            except TooLong:
                if gpu is not None and lane_name_ == "npu":
                    too_long.append((position, chunk))
                    print(f"  [{done}/{len(futs)}] {lane_name_} {tag}: too long "
                          f"for the NPU, requeued for the GPU phase")
                else:
                    errors[0] += 1
                    print(f"map failed on {lane_name_} ({tag}): too long",
                          file=sys.stderr)
                continue
            except Exception as e:
                errors[0] += 1
                print(f"map failed on {lane_name_} ({tag}): {e}", file=sys.stderr)
                continue
            summaries[position] = summary
            writer.add(key, summary)
            print(f"  [{done}/{len(futs)}] {lane_name_} {tag}: "
                  f"{summary[:60]!r}...")

    npu_jobs = []          # (position, zero-arg callable, tag, key, chunk)
    gpu_jobs = []
    pos = 0
    if npu:
        for c in npu_queue:
            pos += 1
            key = chunk_key(npu.model, a.question, c["text"])
            if key in done_keys:
                summaries[pos] = done_keys[key]
                reused += 1
                continue
            npu_jobs.append((pos, (lambda c=c: run_npu(c)), c["id"], key, c))
    if gpu:
        for call in gpu_calls:
            pos += 1
            text = "\n\n".join(c["text"] for c in call)
            key = chunk_key(gpu.model, a.question, text)
            if key in done_keys:
                summaries[pos] = done_keys[key]
                reused += 1
                continue
            tag = "+".join(c["id"] for c in call)
            gpu_jobs.append((pos, (lambda call=call: run_gpu(call)), tag, key, None))

    if reused:
        print(f"resume: reused {reused} checkpointed summar"
              f"{'y' if reused == 1 else 'ies'} from {os.path.basename(ckpt_file)}")
    if npu and npu_jobs:
        with cf.ThreadPoolExecutor(max_workers=npu.concurrent) as ex:
            drain(ex, npu_jobs, "npu")
    # chunks the NPU refused join the GPU phase, keeping their plan position
    # (fractional sub-positions if the GPU budget forces a re-split)
    for position, c in too_long:
        parts = resplit([c], gpu.usable_tokens())
        for j, part in enumerate(parts):
            sub = position + j / (len(parts) + 1)
            key = chunk_key(gpu.model, a.question, part["text"])
            if key in done_keys:
                summaries[sub] = done_keys[key]
                continue
            gpu_jobs.append((sub, (lambda call=[part]: run_gpu(call)),
                             part["id"], key, None))
    if gpu and gpu_jobs:
        with cf.ThreadPoolExecutor(max_workers=gpu.concurrent) as ex:
            drain(ex, gpu_jobs, "gpu")
    writer.close()
    if errors[0]:
        print(f"warn: {errors[0]}/{len(npu_jobs) + len(gpu_jobs)} map calls "
              f"failed (reducing over the survivors)", file=sys.stderr)
    if not summaries:
        sys.exit("error: every map call failed - check lane status in the panel")
    ordered = [summaries[p] for p in sorted(summaries)]
    print(f"map phase: {len(ordered)} summaries in {time.time() - t_map:.1f}s")

    # ---- reduce on the GPU lane (widest window); NPU-only mode uses NPU
    reducer = gpu or npu
    answer = hierarchical_reduce(reducer, a.question, ordered, a.temp,
                                 a.map_max_tokens, a.final_max_tokens,
                                 verbose=a.verbose)
    dt = time.time() - t_start

    with open(a.out, "w") as f:
        f.write(answer)
    manifest_path = a.manifest or (a.out + ".manifest.json")
    manifest = {
        "question": a.question,
        "input_files": [p for p, _ in inputs],
        "input_tokens_est": total_in_toks,
        "chunk_tokens": a.chunk_tokens,
        "chunks": len(chunks),
        "chunks_npu": n_npu,
        "chunks_gpu": n_gpu_chunks,
        "map_errors": errors[0],
        "resumed_summaries": reused,
        "wall_s": round(dt, 1),
        "lanes": {l.name: {"model": l.model, "ctx": l.ctx, "calls": l.calls,
                           "prompt_tokens": l.prompt_toks,
                           "completion_tokens": l.completion_toks,
                           "wall_s": round(l.wall, 1)}
                  for l in lanes},
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n=== done in {dt:.1f}s ===")
    for lane in lanes:
        if lane.calls:
            tps = lane.completion_toks / lane.wall if lane.wall else 0
            print(f"  {lane.name}: {lane.calls} calls, "
                  f"{lane.prompt_toks:,} prompt + {lane.completion_toks:,} "
                  f"completion tokens, {lane.wall:.1f}s wall ({tps:.1f} t/s)")
    print(f"answer written to {a.out} ({len(answer)} chars)")
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()