#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${GREEN_ENV:-/opt/conda/envs/sglang-rocm-v051p2}"
PY="$GREEN_ENV/bin/python"
MODEL="${SGLANG_MODEL_PATH:-$ROOT/taide-12b}"
HOST=127.0.0.1
PORT=8100
SERVED="${SGLANG_SERVED_MODEL:-taide-12b}"
CTX="${SGLANG_CONTEXT_LENGTH:-32768}"
MEM="${SGLANG_MEM_FRACTION_STATIC:-0.30}"
MAXRUN="${SGLANG_MAX_RUNNING_REQUESTS:-8}"

LOGDIR="$ROOT/ops/runtime/logs"
PIDDIR="$ROOT/ops/runtime/pids"
LOG="$LOGDIR/sglang-green-r3-8100.log"
PIDFILE="$PIDDIR/sglang-green-r3-8100.pid"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

[[ -x "$PY" ]] || die "Green runtime missing: $PY"
[[ -f "$MODEL/config.json" ]] || die "TAIDE model missing: $MODEL"
listener 8000 || die "Blue :8000 must remain up"
if listener 8100; then die ":8100 already listening"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p active"; fi
done

export SGLANG_USE_AITER=1
export ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"

"$PY" "$(dirname "$0")/r3_verify_runtime.py"

HELP="$("$PY" -m sglang.launch_server --help 2>&1 || true)"

need_flag() {
  grep -q -- "$1" <<<"$HELP" || die "required SGLang flag missing: $1"
}

for f in --model-path --host --port --dtype --context-length --mem-fraction-static; do
  need_flag "$f"
done

ARGS=(
  -m sglang.launch_server
  --model-path "$MODEL"
  --host "$HOST"
  --port "$PORT"
  --dtype bfloat16
  --context-length "$CTX"
  --mem-fraction-static "$MEM"
)

if grep -q -- "--served-model-name" <<<"$HELP"; then
  ARGS+=(--served-model-name "$SERVED")
fi

if grep -q -- "--max-running-requests" <<<"$HELP"; then
  ARGS+=(--max-running-requests "$MAXRUN")
fi

# Gemma3 has interleaved attention; Triton backend is the conservative
# compatibility choice while AITER remains enabled for ROCm ops/norms.
if grep -q -- "--attention-backend" <<<"$HELP"; then
  ARGS+=(--attention-backend triton)
fi

if grep -q -- "--disable-cuda-graph" <<<"$HELP"; then
  ARGS+=(--disable-cuda-graph)
fi

if grep -q -- "--enable-metrics" <<<"$HELP"; then
  ARGS+=(--enable-metrics)
fi

if grep -q -- "--disable-multimodal" <<<"$HELP"; then
  ARGS+=(--disable-multimodal)
fi

mkdir -p "$LOGDIR" "$PIDDIR"

echo "===== SGLang R3 Green :8100 ====="
echo "python=$PY"
echo "model=$MODEL"
echo "SGLANG_USE_AITER=$SGLANG_USE_AITER"
echo "context=$CTX"
echo "mem_fraction_static=$MEM"
echo "max_running_requests=$MAXRUN"
echo "attention_backend=triton if supported"
echo "cuda_graph=disabled if supported"
echo "log=$LOG"

nohup "$PY" "${ARGS[@]}" >"$LOG" 2>&1 &
PID=$!
printf '%s\n' "$PID" >"$PIDFILE"

for _ in $(seq 1 240); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] SGLang Green exited during startup"
    tail -220 "$LOG" || true
    exit 3
  fi

  if listener 8100; then
    if curl -fsS --max-time 5 http://127.0.0.1:8100/v1/models >/dev/null 2>&1; then
      echo "[READY] SGLang R3 Green :8100"
      exit 0
    fi
  fi
  sleep 2
done

echo "[ERROR] startup timeout"
tail -240 "$LOG" || true
exit 4
