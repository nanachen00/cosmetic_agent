#!/usr/bin/env bash
# Explicit role selection prevents the former all-in-one deployment from being
# started accidentally on either the Mac or the GPU host.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${BLINK_RUNTIME_ROLE:-}" in
  mac) exec "$SCRIPT_DIR/start-mac-production.sh" ;;
  mi300x) exec "$SCRIPT_DIR/start-mi300x-models.sh" ;;
  *)
    echo '[ERROR] 設定 BLINK_RUNTIME_ROLE=mac 或 BLINK_RUNTIME_ROLE=mi300x；不再支援隱式 all-in-one 啟動' >&2
    exit 2
    ;;
esac
