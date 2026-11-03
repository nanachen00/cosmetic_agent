#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
PROFILE_DIR="${PROFILE_DIR:-$ROOT/ops/backend-api-profile}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$PROFILE_DIR/restart/$STAMP"

mkdir -p "$RUN_DIR"

STOP="$ROOT/ops/runtime/stop-production.sh"
START="$ROOT/ops/runtime/start-production.sh"
STATUS="$ROOT/ops/runtime/status-production.sh"

for file in "$STOP" "$START" "$STATUS"; do
  [[ -x "$file" || -f "$file" ]] || {
    echo "[ERROR] Missing canonical runtime script: $file" >&2
    exit 1
  }
done

{
  echo "timestamp_utc=$(date -u --iso-8601=seconds)"
  echo "root=$ROOT"
  echo "production_ports=8000,8001,8002,8003,8010,8011"
  echo "retired_ports=8004,8005"
  echo "green_candidate_8200=NOT_TOUCHED"
  echo
  echo "===== PRE-RESTART LISTENERS ====="
  ss -ltnp 2>/dev/null | grep -E ':(8000|8001|8002|8003|8004|8005|8010|8011|8200)\b' || true
} > "$RUN_DIR/pre-restart.txt"

echo "============================================================"
echo "STOP canonical production services"
echo "============================================================"
bash "$STOP" 2>&1 | tee "$RUN_DIR/stop-production.log"

echo
echo "============================================================"
echo "START canonical production services"
echo "============================================================"
bash "$START" 2>&1 | tee "$RUN_DIR/start-production.log"

echo
echo "============================================================"
echo "STATUS"
echo "============================================================"
bash "$STATUS" 2>&1 | tee "$RUN_DIR/status-production.log"

{
  echo "===== POST-RESTART LISTENERS ====="
  ss -ltnp 2>/dev/null | grep -E ':(8000|8001|8002|8003|8004|8005|8010|8011|8200)\b' || true
  echo
  echo "===== 8010 HEALTH ====="
  curl -fsS --max-time 10 http://127.0.0.1:8010/api/v1/health || true
  echo
  echo
  echo "===== 8011 HEALTH ====="
  curl -fsS --max-time 10 http://127.0.0.1:8011/api/v1/health || true
  echo
} > "$RUN_DIR/post-restart.txt"

failed=0
for port in 8000 8001 8002 8003 8010 8011; do
  if ! ss -ltn 2>/dev/null | grep -q ":${port} "; then
    echo "[ERROR] :$port is not listening after restart" | tee -a "$RUN_DIR/verification.log"
    failed=1
  else
    echo "[OK] :$port listening" | tee -a "$RUN_DIR/verification.log"
  fi
done

for port in 8004 8005; do
  if ss -ltn 2>/dev/null | grep -q ":${port} "; then
    echo "[ERROR] retired :$port unexpectedly listening" | tee -a "$RUN_DIR/verification.log"
    failed=1
  else
    echo "[OK] retired :$port remains down" | tee -a "$RUN_DIR/verification.log"
  fi
done

echo
echo "Restart logs: $RUN_DIR"

if ((failed)); then
  exit 1
fi
