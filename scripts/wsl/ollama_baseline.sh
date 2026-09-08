#!/bin/bash
# Ollama CPU-only baselines via REST API. Windows Ollama server is at
# localhost:11434 (WSL2 mirrors localhost). Tests each model at multiple
# context sizes and measures prompt-eval + eval token/s.
set -u
LOG="/home/fabien/llmnpu/benchmarks/ollama_baseline.jsonl"
API="http://localhost:11434/api/generate"

run_one () {
  local model="$1" ctx="$2"
  local payload resp
  # metrics are in the final response JSON
  payload="{\"model\":\"$model\",\"prompt\":\"Summarize the history of computing in one paragraph.\",\"num_predict\":192,\"num_ctx\":$ctx,\"options\":{\"temperature\":0},\"stream\":false}"
  resp=$(curl -s --max-time 900 -d "$payload" "$API" 2>/dev/null) || { echo "FAIL $model ctx=$ctx"; return 1; }
  # Extract metrics
  pps=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); pe=d.get('prompt_eval_count',0); pet=d.get('prompt_eval_duration',0)/1e9 if d.get('prompt_eval_duration') else 0; print(f\"{pe},{pet},{pe/pet if pet and pe else 0:.2f}\")" 2>/dev/null)
  eps=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); ec=d.get('eval_count',0); et=d.get('eval_duration',0)/1e9 if d.get('eval_duration') else 0; print(f\"{ec},{et},{ec/et if et and ec else 0:.2f}\")" 2>/dev/null)
  loadt=$(echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d.get('load_duration',0)/1e9:.2f}\")" 2>/dev/null)
  echo "$model ctx=$ctx | load ${loadt}s | prompt-eval: $(echo $pps | cut -d, -f1) tok in $(echo $pps | cut -d, -f2)s = $(echo $pps | cut -d, -f3) t/s | eval: $(echo $eps | cut -d, -f1) tok in $(echo $eps | cut -d, -f2)s = $(echo $eps | cut -d, -f3) t/s"
  echo "{\"model\":\"$model\",\"ctx\":$ctx,\"load_s\":$loadt,\"ptok\":$(echo $pps|cut -d, -f1),\"psec\":$(echo $pps|cut -d, -f2),\"pps\":$(echo $pps|cut -d, -f3),\"etok\":$(echo $eps|cut -d, -f1),\"esec\":$(echo $eps|cut -d, -f2),\"eps\":$(echo $eps|cut -d, -f3)}" >> "$LOG"
}

mkdir -p /home/fabien/llmnpu/benchmarks
: > "$LOG"

# Models with context sweep. num_predict kept modest (192) to keep runtime sane;
# context sweep exercises KV-cache allocation & swap behavior.
run_one "qwen2.5-coder:1.5b" 8192
run_one "qwen2.5-coder:1.5b" 32768
run_one "qwen3:8b"           8192
run_one "qwen3:8b"           32768
run_one "mistral-nemo:12b"    8192
run_one "mistral-nemo:12b"    32768
run_one "gpt-oss:20b"         8192
run_one "gpt-oss:20b"         32768
run_one "qwen3-coder:30b"     8192
run_one "qwen3-coder:30b"     32768

echo "=== baselines done ==="