#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-/var/lib/jenkins/tomod/ops/8000-vs-8200-capacity/$STAMP}"

python3 "$HERE/compare_8000_8200_multiturn.py" \
  --blue-url "${BLUE_URL:-http://127.0.0.1:8000}" \
  --green-url "${GREEN_URL:-http://127.0.0.1:8200}" \
  --vu-list "${VU_LIST:-1,2,4,8,16,32,64,128,256,512,1024,2048}" \
  --request-timeout "${REQUEST_TIMEOUT:-30}" \
  --max-tokens "${MAX_TOKENS:-64}" \
  --min-conversations-per-level "${MIN_CONVERSATIONS_PER_LEVEL:-8}" \
  --slo-p95 "${SLO_P95:-15}" \
  --success-floor "${SUCCESS_FLOOR:-0.99}" \
  --conversation-floor "${CONVERSATION_FLOOR:-0.95}" \
  --hard-stop-success "${HARD_STOP_SUCCESS:-0.50}" \
  --hard-stop-conversation "${HARD_STOP_CONVERSATION:-0.25}" \
  --output-dir "$OUT"

echo "Artifacts: $OUT"
