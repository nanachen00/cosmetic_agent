#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${ROOT:-/var/lib/jenkins/tomod}"; PORT="${SGLANG_PORT:-8100}"; PIDFILE="$ROOT/ops/runtime/pids/sglang-green-${PORT}.pid"; LOG="$ROOT/ops/runtime/logs/sglang-green-${PORT}.log"
echo '===== LISTENERS ====='; ss -ltnp 2>/dev/null | grep -E ':(8000|8100)\b' || true
echo; echo '===== PIDFILE ====='
if [[ -f "$PIDFILE" ]]; then PID="$(cat "$PIDFILE")"; echo "pid=$PID"; if [[ -d /proc/$PID ]]; then echo -n 'exe='; readlink -f "/proc/$PID/exe" || true; echo -n 'cmd='; tr '\0' ' ' < "/proc/$PID/cmdline" || true; echo; fi; else echo 'no pidfile'; fi
echo; echo '===== /v1/models ====='; curl -sS --max-time 5 -w '\nHTTP %{http_code}\n' "http://127.0.0.1:${PORT}/v1/models" || true
echo; echo '===== /metrics sample ====='; curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/metrics" 2>/dev/null | head -40 || true
echo; echo '===== GPU ====='; command -v amd-smi >/dev/null 2>&1 && amd-smi metric -g 0 -u -m || true
echo; echo '===== LOG tail ====='; [[ -f "$LOG" ]] && tail -80 "$LOG" || echo "no log: $LOG"
