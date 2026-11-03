#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-/var/lib/jenkins/tomod/ops/8000-vs-8200-capacity/smoke-$STAMP}"

python3 "$HERE/compare_8000_8200_multiturn.py" \
  --vu-list "1,2,4,8" \
  --request-timeout 30 \
  --min-conversations-per-level 4 \
  --output-dir "$OUT"
