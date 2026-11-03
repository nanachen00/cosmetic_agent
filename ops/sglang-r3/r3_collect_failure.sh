#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"
PY="$GREEN_ENV/bin/python"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$ROOT/ops/sglang-green/r3-failure-$STAMP}"
mkdir -p "$OUT"

ss -ltnp > "$OUT/listeners.txt" 2>&1 || true
amd-smi metric -g 0 -u -m > "$OUT/amd-smi.txt" 2>&1 || true
tail -400 "$ROOT/ops/runtime/logs/sglang-green-r3-8100.log" > "$OUT/sglang-log-tail.txt" 2>&1 || true

if [[ -x "$PY" ]]; then
  "$PY" -m pip freeze > "$OUT/pip-freeze.txt" 2>&1 || true
  "$PY" -m pip check > "$OUT/pip-check.txt" 2>&1 || true
  SGLANG_USE_AITER=1 "$PY" "$(dirname "$0")/r3_verify_runtime.py" > "$OUT/runtime-verify.txt" 2>&1 || true
fi

echo "$OUT"
