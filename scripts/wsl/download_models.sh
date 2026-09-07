#!/bin/bash
# Download Q4_0/Q5_0 GGUF models from HuggingFace for Adreno GPU serving.
# Resumable per-file; re-runs skip completed files.
# Sizes (Q4-ish): 1.5B ~1GB | 8B ~5GB | 12B ~7GB | 20B ~12GB | 30B ~17GB
set -u
ROOT="${LLMNPU_ROOT:-$HOME/llmnpu}"

# resolve the Windows-side models dir the same way panel.py does
WIN_ROOT="${LLMNPU_WIN_ROOT:-}"
if [ -z "$WIN_ROOT" ]; then
    for u in /mnt/c/Users/*/llmnpu; do
        [ -d "$u" ] && WIN_ROOT="$u" && break
    done
fi
DEST="${WIN_ROOT:-$ROOT}/models"
mkdir -p "$DEST"
LOG="$ROOT/logs/downloads.log"

dl () {
  local url="$1" out="$2"
  if [ -s "$DEST/$out" ]; then
    echo "[skip] $out exists" | tee -a "$LOG"
    return 0
  fi
  echo "[get ] $out" | tee -a "$LOG"
  curl -L --fail -C - --retry 60 --retry-all-errors --retry-delay 3 \
       --connect-timeout 20 --progress-bar -o "$DEST/$out" "$url" 2>>"$LOG" \
    && echo "[done] $out $(du -h "$DEST/$out" | cut -f1)" | tee -a "$LOG" \
    || { echo "[FAIL] $out" | tee -a "$LOG"; return 1; }
}

# Qwen2.5-Coder-1.5B-Instruct Q4_0 — fast, light
dl "https://huggingface.co/bartowski/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-1.5B-Instruct-Q4_0.gguf" \
   "qwen2.5-coder-1.5b-Q4_0.gguf"

# Qwen3-8B Q5_0 (official Qwen repo)
dl "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q5_0.gguf" \
   "qwen3-8b-Q5_0.gguf"

# Mistral-Nemo-Instruct-2407 Q4_0 — 12B dense
dl "https://huggingface.co/bartowski/Mistral-Nemo-Instruct-2407-GGUF/resolve/main/Mistral-Nemo-Instruct-2407-Q4_0.gguf" \
   "mistral-nemo-12b-Q4_0.gguf"

# gpt-oss-20b Q4_0 — MoE (fast for its size)
dl "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_0.gguf" \
   "gpt-oss-20b-Q4_0.gguf"

# Qwen3-Coder-30B-A3B-Instruct Q4_0 — MoE, only ~3B active: flagship coder, 30+ tok/s
dl "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_0.gguf" \
   "qwen3-coder-30b-Q4_0.gguf"

echo "=== all done ===" | tee -a "$LOG"