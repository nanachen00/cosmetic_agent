#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${RUN_DIR:?Set RUN_DIR to the exact public runs/<timestamp>; never .state}"
exec "${PYTHON_BIN:-python3}" -I -B "$HERE/bench.py" pack --run-dir "$RUN_DIR"
