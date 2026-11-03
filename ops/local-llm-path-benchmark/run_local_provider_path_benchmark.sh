#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# Default is read-only preflight. Full test requires the explicit run command.
if (($#==0)); then set -- preflight; fi
exec "$PYTHON_BIN" -I -B "$HERE/bench.py" "$@"
