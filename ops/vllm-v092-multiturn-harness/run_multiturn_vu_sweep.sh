#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

URL="${URL:-http://127.0.0.1:8200}"
VU_LIST="${VU_LIST:-1,2,4,8,16,32,64}"
TURNS="${TURNS:-4}"
GPU="${GPU:-0}"
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-1.0}"
PAUSE_BETWEEN="${PAUSE_BETWEEN:-5}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/results-$STAMP}"

exec python3 "$HERE/benchmark_multiturn_chat.py" \
  --url "$URL" \
  --vu-list "$VU_LIST" \
  --turns "$TURNS" \
  --gpu "$GPU" \
  --telemetry required \
  --telemetry-interval "$TELEMETRY_INTERVAL" \
  --pause-between "$PAUSE_BETWEEN" \
  --label "VLLM8200-MT" \
  --output-dir "$OUT"
