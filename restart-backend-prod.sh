#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME="/var/lib/jenkins/tomod/ops/runtime"
echo "[DEPRECATED] $(basename "$0") now delegates to the unified runtime launcher"
exec "$RUNTIME/restart-production.sh" "$@"
