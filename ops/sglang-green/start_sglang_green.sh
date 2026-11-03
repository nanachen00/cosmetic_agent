#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm}"
PY="$GREEN_ENV/bin/python"
MODEL="${SGLANG_MODEL_PATH:-$ROOT/taide-12b}"
PORT=8100
HOST=127.0.0.1
SERVED="${SGLANG_SERVED_MODEL:-taide-12b}"
CTX="${SGLANG_CONTEXT_LENGTH:-32768}"
MEM="${SGLANG_MEM_FRACTION_STATIC:-0.30}"
MAXRUN="${SGLANG_MAX_RUNNING_REQUESTS:-8}"
PROFILE="${SGLANG_PROFILE:-compat}"

LOGDIR="$ROOT/ops/runtime/logs"
PIDDIR="$ROOT/ops/runtime/pids"
LOG="$LOGDIR/sglang-green-8100.log"
PIDFILE="$PIDDIR/sglang-green-8100.pid"

die() { echo "[ERROR] $*" >&2; exit 2; }

[[ -x "$PY" ]] || die "Green Python missing: $PY"
[[ -f "$MODEL/config.json" ]] || die "model missing: $MODEL"
if ss -H -ltn "sport = :8100" 2>/dev/null | grep -q .; then die ":8100 already in use"; fi
if ! ss -H -ltn "sport = :8000" 2>/dev/null | grep -q .; then die "Blue :8000 must remain up"; fi
for p in 8004 8005; do
  if ss -H -ltn "sport = :$p" 2>/dev/null | grep -q .; then die "retired/stale :$p is active"; fi
done

"$PY" "$(dirname "$0")/verify_sglang_green_runtime.py"

HELP="$("$PY" -m sglang.launch_server --help 2>&1 || true)"
require_flag() {
  grep -q -- "$1" <<<"$HELP" || die "installed SGLang does not expose required flag $1"
}
for f in --model-path --host --port --dtype --mem-fraction-static --context-length; do
  require_flag "$f"
done

ARGS=(
  -m sglang.launch_server
  --model-path "$MODEL"
  --host "$HOST"
  --port "$PORT"
  --dtype bfloat16
  --mem-fraction-static "$MEM"
  --context-length "$CTX"
)

if grep -q -- "--served-model-name" <<<"$HELP"; then
  ARGS+=(--served-model-name "$SERVED")
fi
if grep -q -- "--max-running-requests" <<<"$HELP"; then
  ARGS+=(--max-running-requests "$MAXRUN")
fi
if grep -q -- "--attention-backend" <<<"$HELP"; then
  ARGS+=(--attention-backend triton)
fi
if grep -q -- "--enable-metrics" <<<"$HELP"; then
  ARGS+=(--enable-metrics)
fi
if [[ "$PROFILE" == "compat" ]] && grep -q -- "--disable-cuda-graph" <<<"$HELP"; then
  ARGS+=(--disable-cuda-graph)
elif [[ "$PROFILE" != "compat" && "$PROFILE" != "perf" ]]; then
  die "SGLANG_PROFILE must be compat or perf"
fi

mkdir -p "$LOGDIR" "$PIDDIR"

echo "===== SGLang Green :8100 ====="
echo "python=$PY"
echo "model=$MODEL"
echo "context=$CTX"
echo "mem_fraction_static=$MEM"
echo "profile=$PROFILE"
echo "attention_backend=triton if supported"
echo "SGLANG_USE_AITER=0"
echo "log=$LOG"

# Compatibility gate deliberately avoids AITER. If this pinned ROCm build requires
# vLLM/AITER imports, startup should fail visibly rather than installing them silently.
export SGLANG_USE_AITER=0

nohup "$PY" "${ARGS[@]}" >"$LOG" 2>&1 &
PID=$!
printf '%s\n' "$PID" >"$PIDFILE"

for _ in $(seq 1 180); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] SGLang exited during startup"
    tail -160 "$LOG" || true
    exit 3
  fi
  if ss -H -ltn "sport = :8100" 2>/dev/null | grep -q .; then
    if curl -fsS --max-time 5 http://127.0.0.1:8100/v1/models >/dev/null 2>&1; then
      echo "[READY] Green :8100"
      exit 0
    fi
  fi
  sleep 2
done

echo "[ERROR] timeout waiting for Green /v1/models"
tail -200 "$LOG" || true
exit 4
