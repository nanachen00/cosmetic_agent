#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/lib/jenkins/tomod/cosmetic-agent"
PID_FILE="$ROOT/logs/firecrawl_scheduler.pid"
mkdir -p "$ROOT/logs"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(tr -cd '0-9' < "$PID_FILE")"
  if [[ -n "$old_pid" && -r "/proc/$old_pid/cmdline" ]] && grep -aq "firecrawl_weekly_scheduler.py" "/proc/$old_pid/cmdline"; then
    kill "$old_pid" || true
    for _ in {1..20}; do
      kill -0 "$old_pid" 2>/dev/null || break
      sleep 0.25
    done
  fi
fi

cd "$ROOT"
nohup python scripts/firecrawl_weekly_scheduler.py >> logs/firecrawl_scheduler.log 2>&1 &
new_pid=$!
printf '%s\n' "$new_pid" > "$PID_FILE"
sleep 1
kill -0 "$new_pid"
echo "BLINK content scheduler started: PID $new_pid"
