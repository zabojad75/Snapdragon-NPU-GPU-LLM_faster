#!/usr/bin/env python3
"""Call the local GPU-served qwen3-coder model and report generation speed.

Usage:
  python3 qwen_gen.py --prompt "..." --out /tmp/out.txt [--max-tokens 2000] [--temp 0.2] [--model ...]
  python3 qwen_gen.py --prompt-file prompt.txt --out /tmp/out.txt
"""
import argparse, json, subprocess, sys, time, urllib.request

def gateway():
    out = subprocess.run(["ip", "route", "show", "default"],
                         capture_output=True, text=True).stdout
    for w in out.split():
        if "." in w and not w.startswith("(unnamed"):
            pass
    # first token that looks like an IP after 'default via'
    parts = out.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="qwen3-coder-30b-Q4_0")
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--temp", type=float, default=0.2)
    ap.add_argument("--system", default="You are an expert frontend engineer. "
                    "Output ONLY the requested code inside a single fenced code block. "
                    "No explanations, no commentary, no prose before or after the code.")
    a = ap.parse_args()

    if a.prompt_file:
        prompt = open(a.prompt_file).read()
    elif a.prompt:
        prompt = a.prompt
    else:
        prompt = sys.stdin.read()

    gw = gateway()
    if not gw:
        gw = "localhost"
    url = f"http://{gw}:8081/v1/chat/completions"
    body = {
        "model": a.model,
        "messages": [
            {"role": "system", "content": a.system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": a.max_tokens,
        "temperature": a.temp,
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code}: {e.read().decode()}\n")
        sys.exit(1)
    dt = time.time() - t0
    d = json.loads(raw)
    usage = d.get("usage", {})
    msg = d["choices"][0]["message"]
    content = msg.get("content") or ""
    if msg.get("reasoning_content"):
        content = msg["reasoning_content"] + "\n" + content
    ct = usage.get("completion_tokens", 0)
    pt = usage.get("prompt_tokens", 0)
    tps = ct / dt if dt > 0 else 0
    with open(a.out, "w") as f:
        f.write(content)
    print(f"model={a.model} prompt_tokens={pt} completion_tokens={ct} "
          f"time={dt:.1f}s tps={tps:.1f} t/s")
    print(f"wrote {a.out} ({len(content)} chars)")

if __name__ == "__main__":
    main()
