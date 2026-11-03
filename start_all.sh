#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/var/lib/jenkins/tomod"
RUNTIME="$ROOT/ops/runtime"

echo "[1/3] Restarting unified backend services"
"$RUNTIME/restart-production.sh"

echo "[2/3] Restarting frontend"
"$ROOT/restart_frontend.sh"

echo "[3/3] Restarting content scheduler"
if [[ -x "$ROOT/cosmetic-agent/restart_content_scheduler.sh" ]]; then
    "$ROOT/cosmetic-agent/restart_content_scheduler.sh"
else
    echo "[INFO] Content scheduler launcher is not installed"
fi

"$RUNTIME/status-production.sh"
