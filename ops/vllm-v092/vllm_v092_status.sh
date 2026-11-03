#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"
LOG="$ROOT/ops/runtime/logs/vllm-v092-green-8200.log"
PIDFILE="$ROOT/ops/runtime/pids/vllm-v092-green-8200.pid"

echo "===== PORTS ====="
ss -ltnp 2>/dev/null | grep -E ':(8000|8200)\b' || true

echo
echo "===== PID ====="
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  echo "PID=$PID"
  if [[ -d "/proc/$PID" ]]; then
    echo -n "exe="; readlink -f "/proc/$PID/exe" || true
    echo -n "cwd="; readlink -f "/proc/$PID/cwd" || true
    echo -n "cmd="; tr '\0' ' ' < "/proc/$PID/cmdline" || true; echo
  fi
else
  echo "no pidfile"
fi

echo
echo "===== /v1/models ====="
curl -sS --max-time 5 -w '\nHTTP %{http_code}\n' \
  http://127.0.0.1:8200/v1/models || true

echo
echo "===== GPU ====="
amd-smi metric -g 0 -u -m 2>/dev/null || true

echo
echo "===== LOG tail ====="
tail -120 "$LOG" 2>/dev/null || true
