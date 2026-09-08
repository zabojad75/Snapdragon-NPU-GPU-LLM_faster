#!/bin/bash
# Resume qwen3-coder-30b download — slow & steady for metered/unstable connection
# Usage: bash resume_30b.sh  (safe to re-run; resumes from where the file stopped)
URL="https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_0.gguf"
OUT="/mnt/c/Users/fabie/llmnpu/models/qwen3-coder-30b-Q4_0.gguf"
TOTAL=17379990688
LOG="/home/fabien/llmnpu/logs/resume_30b.log"

have=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
if [ "$have" -ge "$TOTAL" ]; then echo "Already complete ($have bytes)"; exit 0; fi
echo "Have $have / $TOTAL bytes; resuming at 2 MB/s..." | tee -a "$LOG"

# -C - : resume | --limit-rate 2M : ~16 Mbps steady | retry forever-ish on drops
curl -L --fail -C - --limit-rate 2M \
     --retry 60 --retry-all-errors --retry-delay 5 \
     --connect-timeout 20 --speed-time 60 --speed-limit 10000 \
     -o "$OUT" "$URL" >> "$LOG" 2>&1
rc=$?
end=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
echo "curl exit=$rc size=$end/$TOTAL" | tee -a "$LOG"
[ "$end" -ge "$TOTAL" ] && echo "DOWNLOAD COMPLETE" | tee -a "$LOG"