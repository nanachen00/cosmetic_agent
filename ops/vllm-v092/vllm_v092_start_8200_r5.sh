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

LOGDIR="$ROOT/ops/runtime/logs"
PIDDIR="$ROOT/ops/runtime/pids"
LOG="$LOGDIR/vllm-v092-green-8200.log"
PIDFILE="$PIDDIR/vllm-v092-green-8200.pid"

die() { echo "[ERROR] $*" >&2; exit 2; }
listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }

[[ -x "$PY" ]] || die "Green Python missing"
[[ -x "$VLLM_BIN" ]] || die "vLLM CLI missing"
[[ -f "$MODEL/config.json" ]] || die "TAIDE model missing"

listener 8000 || die "Blue :8000 must remain listening"
if listener "$PORT"; then die ":$PORT already occupied"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p unexpectedly active"; fi
done

if [[ -f "$PIDFILE" ]]; then
  OLD="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ "$OLD" =~ ^[0-9]+$ ]] && [[ -d "/proc/$OLD" ]]; then
    die "PID file points to live PID=$OLD"
  fi
  rm -f "$PIDFILE"
fi

TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export VLLM_TARGET_DEVICE=rocm

echo "===== TEXT-ONLY RUNTIME PREFLIGHT ====="
"$PY" - <<'PY'
import torch, triton, vllm, vllm._C, vllm._rocm_C
from triton.compiler.compiler import triton_key
from triton.runtime import driver
from vllm.platforms import current_platform

target = driver.active.get_current_target()
print("torch:", torch.__version__)
print("hip:", torch.version.hip)
print("gpu:", torch.cuda.get_device_name(0))
print("vllm:", vllm.__version__)
print("platform:", type(current_platform).__name__)
print("triton:", triton.__version__)
print("triton_key:", triton_key)
print("triton_target:", target)

if not current_platform.is_rocm():
    raise SystemExit("[ERROR] vLLM platform is not ROCm")
if getattr(target, "backend", None) != "hip" or getattr(target, "arch", None) != "gfx942":
    raise SystemExit("[ERROR] Triton is not HIP/gfx942")
PY

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
  --limit-mm-per-prompt '{"image":0}'
)

case "$PROFILE" in
  compat) ARGS+=(--enforce-eager) ;;
  perf) ;;
  *) die "VLLM_PROFILE must be compat or perf" ;;
esac

mkdir -p "$LOGDIR" "$PIDDIR"

echo
echo "===== START vLLM v0.9.2 TEXT-ONLY :$PORT ====="
echo "model=$MODEL"
echo "profile=$PROFILE"
echo "max_model_len=$CTX"
echo "gpu_memory_utilization=$MEM"
echo "max_num_seqs=$MAX_SEQS"
echo "multimodal_image_limit=0"
echo "log=$LOG"

: > "$LOG"
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
  if listener "$PORT" && curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
    echo "[READY] vLLM text-only candidate :$PORT"
    exit 0
  fi
  sleep 2
done

echo "[ERROR] timeout waiting for /v1/models"
tail -360 "$LOG" || true
exit 4
