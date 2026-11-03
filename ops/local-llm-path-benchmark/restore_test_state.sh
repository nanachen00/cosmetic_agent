#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${STATE_DIR:?Set STATE_DIR to the unresolved PRIVATE run directory}"
# New journals only. Never restores or restarts a shared service including :8200.
exec "${PYTHON_BIN:-python3}" -I -B "$HERE/bench.py" recover --state "$STATE_DIR"
