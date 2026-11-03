#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${URL:-http://127.0.0.1:8200}"
TURNS="${TURNS:-4}"
GPU="${GPU:-0}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/results-vu128-$STAMP}"

exec python3 "$HERE/benchmark_multiturn_chat.py" \
  --url "$URL" \
  --vus 128 \
  --turns "$TURNS" \
  --gpu "$GPU" \
  --telemetry required \
  --label "VLLM8200-MT-VU128" \
  --output-dir "$OUT"
