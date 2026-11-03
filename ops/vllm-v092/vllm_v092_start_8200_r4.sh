#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
PY="$GREEN_ENV/bin/python"
VLLM_BIN="$GREEN_ENV/bin/vllm"
MODEL="${TAIDE_MODEL_PATH:-$ROOT/taide-12b}"

HOST="${VLLM_HOST:-127.0.0.1}"
PORT="${VLLM_PORT:-8200}"
SERVED="${VLLM_SERVED_MODEL:-taide-12b}"
CTX="${VLLM_MAX_MODEL_LEN:-32768}"
MEM="${VLLM_GPU_MEMORY_UTILIZATION:-0.30}"
MAX_SEQS="${VLLM_MAX_NUM_SEQS:-8}"
PROFILE="${VLLM_PROFILE:-compat}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="$ROOT/ops/runtime/logs"
PIDDIR="$ROOT/ops/runtime/pids"
LOG="$LOGDIR/vllm-v092-green-8200.log"
PIDFILE="$PIDDIR/vllm-v092-green-8200.pid"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

[[ -x "$PY" ]] || die "Green Python missing: $PY"
[[ -x "$VLLM_BIN" ]] || die "vLLM CLI missing: $VLLM_BIN"
[[ -f "$MODEL/config.json" ]] || die "TAIDE model missing: $MODEL"

listener 8000 || die "Blue :8000 must remain listening"
if listener "$PORT"; then die "candidate :$PORT already occupied"; fi
for q in 8004 8005; do
  if listener "$q"; then die "retired/stale :$q unexpectedly active"; fi
done

if [[ -f "$PIDFILE" ]]; then
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && [[ -d "/proc/$OLD_PID" ]]; then
    echo "[ERROR] existing PID file points to live PID=$OLD_PID"
    tr '\0' ' ' < "/proc/$OLD_PID/cmdline" 2>/dev/null || true
    echo
    exit 2
  fi
  rm -f "$PIDFILE"
fi

TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"
[[ -f "$TORCH_LIB/libc10.so" ]] || die "libc10.so missing: $TORCH_LIB"

export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export VLLM_TARGET_DEVICE=rocm

echo "===== PLATFORM PREFLIGHT ====="
"$PY" - <<'PY'
import torch
from vllm.platforms import current_platform
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
print("platform:", type(current_platform).__name__)
print("is_rocm:", current_platform.is_rocm())
if not torch.cuda.is_available() or torch.version.hip is None:
    raise SystemExit("[ERROR] Torch ROCm unavailable")
if not current_platform.is_rocm():
    raise SystemExit("[ERROR] vLLM did not resolve RocmPlatform")
PY

if [[ -f "$HERE/vllm_v092_verify_r2.py" ]]; then
  echo
  echo "===== RUNTIME VERIFY ====="
  "$PY" "$HERE/vllm_v092_verify_r2.py"
fi

ARGS=(
  serve
  "$MODEL"
  --host "$HOST"
  --port "$PORT"
  --served-model-name "$SERVED"
  --dtype bfloat16
  --max-model-len "$CTX"
  --gpu-memory-utilization "$MEM"
  --max-num-seqs "$MAX_SEQS"
  --generation-config auto
)

case "$PROFILE" in
  compat) ARGS+=(--enforce-eager) ;;
  perf) ;;
  *) die "VLLM_PROFILE must be compat or perf" ;;
esac

mkdir -p "$LOGDIR" "$PIDDIR"

echo
echo "===== vLLM v0.9.2 candidate :$PORT ====="
echo "CLI=$VLLM_BIN"
echo "model=$MODEL"
echo "served_model=$SERVED"
echo "profile=$PROFILE"
echo "host=$HOST"
echo "port=$PORT"
echo "max_model_len=$CTX"
echo "gpu_memory_utilization=$MEM"
echo "max_num_seqs=$MAX_SEQS"
echo "VLLM_USE_V1=${VLLM_USE_V1:-<default>}"
echo "log=$LOG"

nohup "$VLLM_BIN" "${ARGS[@]}" >"$LOG" 2>&1 &
PID=$!
printf '%s\n' "$PID" >"$PIDFILE"
echo "PID=$PID"

for _ in $(seq 1 240); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] vLLM exited during startup"
    tail -320 "$LOG" || true
    exit 3
  fi
  if listener "$PORT"; then
    if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
      echo "[READY] vLLM candidate :$PORT"
      exit 0
    fi
  fi
  sleep 2
done

echo "[ERROR] timeout waiting for /v1/models"
tail -360 "$LOG" || true
exit 4
