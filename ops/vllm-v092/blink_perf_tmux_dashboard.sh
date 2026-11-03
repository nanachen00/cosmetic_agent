#!/usr/bin/env bash
set -Eeuo pipefail

SESSION="${SESSION:-vllm-v092}"
WINDOW="${WINDOW:-perf-watch}"
GPU="${GPU:-0}"
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
LOGDIR="${LOGDIR:-$ROOT/ops/runtime/logs/perf-$STAMP}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "[ERROR] missing command: $1" >&2; exit 2; }; }

need tmux
need amd-smi
mkdir -p "$LOGDIR"

# Create the session if it does not already exist.
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" -n control
fi

# Fail closed if the dashboard window already exists.
if tmux list-windows -t "$SESSION" -F '#W' | grep -Fxq "$WINDOW"; then
  echo "[ERROR] tmux window '$SESSION:$WINDOW' already exists."
  echo "Attach with: tmux attach -t '$SESSION'"
  exit 2
fi

cpu_cmd=$(cat <<EOF
LOG='$LOGDIR/cpu.log'
TMP=\$(mktemp)
trap 'rm -f "\$TMP"' EXIT
while true; do
  {
    echo "===== \$(date --iso-8601=seconds) ====="
    if command -v mpstat >/dev/null 2>&1; then
      mpstat 1 1
    else
      top -b -n 1 | head -15
      sleep 1
    fi
    echo
    echo "--- top CPU/RSS processes ---"
    ps -eo pid,ppid,pcpu,pmem,rss,etime,args --sort=-pcpu | head -20
  } >"\$TMP" 2>&1
  cat "\$TMP" >>"\$LOG"
  clear
  cat "\$TMP"
done
EOF
)

gpu_cmd=$(cat <<EOF
LOG='$LOGDIR/gpu-summary.log'
TMP=\$(mktemp)
trap 'rm -f "\$TMP"' EXIT
while true; do
  {
    echo "===== \$(date --iso-8601=seconds) ====="
    amd-smi monitor -g '$GPU' --process
  } >"\$TMP" 2>&1
  cat "\$TMP" >>"\$LOG"
  clear
  cat "\$TMP"
  sleep 1
done
EOF
)

xcp_cmd=$(cat <<EOF
LOG='$LOGDIR/gpu-xcp.log'
TMP=\$(mktemp)
trap 'rm -f "\$TMP"' EXIT
{
  echo "===== START \$(date --iso-8601=seconds) ====="
  amd-smi static -g '$GPU' --partition 2>&1 || true
} >>"\$LOG"
while true; do
  {
    echo "===== \$(date --iso-8601=seconds) ====="
    amd-smi metric -g '$GPU' --usage
  } >"\$TMP" 2>&1
  cat "\$TMP" >>"\$LOG"
  clear
  cat "\$TMP"
  sleep 1
done
EOF
)

vllm_cmd=$(cat <<EOF
LOG='$ROOT/ops/runtime/logs/vllm-v092-green-8200.log'
echo "Watching: \$LOG"
echo "Telemetry logs: $LOGDIR"
echo
exec tail -F "\$LOG"
EOF
)

# Create a 2x2 dashboard.
tmux new-window -d -t "$SESSION:" -n "$WINDOW" "bash -lc $(printf '%q' "$cpu_cmd")"
tmux split-window -h -t "$SESSION:$WINDOW.0" "bash -lc $(printf '%q' "$gpu_cmd")"
tmux split-window -v -t "$SESSION:$WINDOW.0" "bash -lc $(printf '%q' "$xcp_cmd")"
tmux split-window -v -t "$SESSION:$WINDOW.1" "bash -lc $(printf '%q' "$vllm_cmd")"
tmux select-layout -t "$SESSION:$WINDOW" tiled

# Pane labels.
tmux set-option -w -t "$SESSION:$WINDOW" pane-border-status top
tmux select-pane -t "$SESSION:$WINDOW.0" -T "CPU + top processes"
tmux select-pane -t "$SESSION:$WINDOW.1" -T "GPU aggregate + processes"
tmux select-pane -t "$SESSION:$WINDOW.2" -T "GPU XCP / compute partitions"
tmux select-pane -t "$SESSION:$WINDOW.3" -T "vLLM :8200 log"

cat > "$LOGDIR/README.txt" <<EOF
BLINK performance telemetry
Started: $(date --iso-8601=seconds)
tmux: $SESSION:$WINDOW
GPU: $GPU

cpu.log          CPU summary + top processes
gpu-summary.log  AMD SMI aggregate metrics + GPU processes
gpu-xcp.log      AMD SMI per-XCP/partition usage
vLLM log         $ROOT/ops/runtime/logs/vllm-v092-green-8200.log
EOF

echo "[READY] tmux dashboard: $SESSION:$WINDOW"
echo "[READY] telemetry logs: $LOGDIR"
echo
echo "Attach:"
echo "  tmux attach -t '$SESSION'"
echo
echo "Then select window '$WINDOW' with Ctrl+B, W."
