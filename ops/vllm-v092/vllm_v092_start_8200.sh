#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
GREEN_ENV="${VLLM_GREEN_ENV:-/opt/conda/envs/vllm-rocm-v092}"
PY="$GREEN_ENV/bin/python"
MODEL="${TAIDE_MODEL_PATH:-$ROOT/taide-12b}"
HOST=127.0.0.1
PORT=8200
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

[[ -x "$PY" ]] || die "vLLM Green Python missing: $PY"
[[ -f "$MODEL/config.json" ]] || die "TAIDE model missing: $MODEL"
listener 8000 || die "Blue :8000 must remain up"
if listener 8200; then die ":8200 already occupied"; fi
for p in 8004 8005; do
  if listener "$p"; then die "retired/stale :$p active"; fi
done

TORCH_LIB="$("$PY" - <<'PY'
from pathlib import Path
import torch
print(Path(torch.__file__).resolve().parent / "lib")
PY
)"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export VLLM_TARGET_DEVICE=rocm

"$PY" "$(dirname "$0")/vllm_v092_verify.py"

HELP="$("$PY" -m vllm.entrypoints.openai.api_server --help 2>&1 || true)"
need() { grep -q -- "$1" <<<"$HELP" || die "required vLLM flag missing: $1"; }

for flag in --model --host --port --dtype --max-model-len --gpu-memory-utilization --served-model-name; do
  need "$flag"
done

ARGS=(
  -m vllm.entrypoints.openai.api_server
  --model "$MODEL"
  --host "$HOST"
  --port "$PORT"
  --served-model-name "$SERVED"
  --dtype bfloat16
  --max-model-len "$CTX"
  --gpu-memory-utilization "$MEM"
)

if grep -q -- "--max-num-seqs" <<<"$HELP"; then
  ARGS+=(--max-num-seqs "$MAX_SEQS")
fi

case "$PROFILE" in
  compat)
    if grep -q -- "--enforce-eager" <<<"$HELP"; then
      ARGS+=(--enforce-eager)
    fi
    ;;
  perf)
    ;;
  *)
    die "VLLM_PROFILE must be compat or perf"
    ;;
esac

mkdir -p "$LOGDIR" "$PIDDIR"

echo "===== vLLM v0.9.2 candidate :8200 ====="
echo "python=$PY"
echo "model=$MODEL"
echo "profile=$PROFILE"
echo "max_model_len=$CTX"
echo "gpu_memory_utilization=$MEM"
echo "max_num_seqs=$MAX_SEQS"
echo "log=$LOG"

nohup "$PY" "${ARGS[@]}" >"$LOG" 2>&1 &
PID=$!
printf '%s\n' "$PID" >"$PIDFILE"

for _ in $(seq 1 240); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "[ERROR] vLLM exited during startup"
    tail -220 "$LOG" || true
    exit 3
  fi
  if listener 8200; then
    if curl -fsS --max-time 5 http://127.0.0.1:8200/v1/models >/dev/null 2>&1; then
      echo "[READY] vLLM candidate :8200"
      exit 0
    fi
  fi
  sleep 2
done

echo "[ERROR] timeout waiting for /v1/models"
tail -260 "$LOG" || true
exit 4
