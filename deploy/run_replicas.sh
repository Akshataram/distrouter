#!/usr/bin/env bash
# Launches 3 real vLLM replicas serving a Qwen model, one per GPU, on a
# single multi-GPU machine. Requires: NVIDIA drivers, CUDA, and
# `pip install vllm` already done in your GPU environment (not this sandbox).
#
# Usage:
#   MODEL=Qwen/Qwen2.5-7B-Instruct ./deploy/run_replicas.sh
#
# Override MODEL to fit your GPU memory, e.g.:
#   Qwen/Qwen2.5-0.5B-Instruct   (fits on almost anything, good for a smoke test)
#   Qwen/Qwen2.5-1.5B-Instruct
#   Qwen/Qwen2.5-7B-Instruct     (needs ~16-24GB VRAM per replica at fp16)
#   Qwen/Qwen2.5-14B-Instruct    (needs ~32GB+ VRAM per replica)
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
PORTS=(8001 8002 8003)
GPUS=(0 1 2)
LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"

for i in "${!PORTS[@]}"; do
  port="${PORTS[$i]}"
  gpu="${GPUS[$i]}"
  echo "Launching replica $i: model=$MODEL gpu=$gpu port=$port"
  CUDA_VISIBLE_DEVICES="$gpu" nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --port "$port" \
    --enable-prefix-caching \
    --gpu-memory-utilization 0.90 \
    > "$LOG_DIR/replica-$i.log" 2>&1 &
  echo "  pid=$! log=$LOG_DIR/replica-$i.log"
done

echo
echo "Waiting for replicas to report healthy (this can take a few minutes while weights load)..."
for i in "${!PORTS[@]}"; do
  port="${PORTS[$i]}"
  until curl -sf "http://localhost:$port/health" > /dev/null 2>&1; do
    sleep 5
  done
  echo "  replica $i on port $port is healthy"
done

echo
echo "All 3 replicas up. Point SwiftServe at them with:"
echo "  export SWIFTSERVE_REPLICAS=http://localhost:8001,http://localhost:8002,http://localhost:8003"
echo "  export SWIFTSERVE_MODEL=$MODEL"
