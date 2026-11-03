#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-/var/lib/jenkins/tomod/ops/8000-vs-8200-capacity/vllm-limit-$STAMP}"

ARGS=(
  python3 "$HERE/probe_vllm_limit.py"
  --green-url "${GREEN_URL:-http://127.0.0.1:8200}"
  --max-vu "${MAX_VU:-2048}"
  --request-timeout "${REQUEST_TIMEOUT:-30}"
  --max-tokens "${MAX_TOKENS:-64}"
  --slo-p95 "${SLO_P95:-15}"
  --success-floor "${SUCCESS_FLOOR:-0.99}"
  --conversation-floor "${CONVERSATION_FLOOR:-0.95}"
  --refine-steps "${REFINE_STEPS:-8}"
  --output-dir "$OUT"
)
[[ -n "${SEED_SUMMARY:-}" ]] && ARGS+=(--seed-summary "$SEED_SUMMARY")
"${ARGS[@]}"

echo "Artifacts: $OUT"
