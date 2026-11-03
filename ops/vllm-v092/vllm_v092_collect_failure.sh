#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
PY="$GREEN_ENV/bin/python"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$ROOT/ops/vllm-green/failure-$STAMP}"
mkdir -p "$OUT"

ss -ltnp > "$OUT/listeners.txt" 2>&1 || true
amd-smi metric -g 0 -u -m > "$OUT/amd-smi.txt" 2>&1 || true
tail -400 "$ROOT/ops/runtime/logs/vllm-v092-green-8200.log" > "$OUT/vllm-log-tail.txt" 2>&1 || true

if [[ -x "$PY" ]]; then
  "$PY" -m pip freeze > "$OUT/pip-freeze.txt" 2>&1 || true
  "$PY" -m pip check > "$OUT/pip-check.txt" 2>&1 || true
  "$PY" "$(dirname "$0")/vllm_v092_verify.py" > "$OUT/runtime-verify.txt" 2>&1 || true
fi

echo "$OUT"
