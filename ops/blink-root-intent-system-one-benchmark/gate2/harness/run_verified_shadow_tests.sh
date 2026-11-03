#!/usr/bin/env bash
# One orchestration process freezes source/config ONCE for auth, smoke, formal.
set -Eeuo pipefail
umask 077
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ACK=0
for arg in "$@"; do [[ "$arg" != --ack-shared-data-services ]] || ACK=1; done
if [[ "$ACK" != 1 ]]; then
  echo '[REFUSED] --ack-shared-data-services required. Use --auth-mode member --accounts-file .private/accounts.json'
  exit 2
fi
cd "$HERE"
echo "[HARNESS_ROOT] $HERE"
echo "[PATCH] shadow-ab-intent-output-budget"
exec 9> "$HERE/.verified-workflow.lock"
flock -n 9 || { echo '[REFUSED] Another workflow is active'; exit 1; }
sha256sum -c MANIFEST.sha256
LOG_DIR="$HERE/workflows/$("$PYTHON_BIN" -I -B -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))')"
mkdir -p "$LOG_DIR"
echo "[WORKFLOW] $LOG_DIR"
PYTHON_BIN="$PYTHON_BIN" bash "$HERE/selftest.sh" 2>&1 | tee "$LOG_DIR/selftest.log"
# Snapshot capture happens once INSIDE workflow. Do not call separate live-copy
# preflight/auth/smoke/formal commands from this shell wrapper.
set +e
"$PYTHON_BIN" -I -B "$HERE/bench.py" workflow "$@" 2>&1 | tee "$LOG_DIR/workflow.log"
CODES=("${PIPESTATUS[@]}")
set -e
printf '%s\n' "${CODES[0]}" > "$LOG_DIR/exit-code.txt"
if [[ "${CODES[1]}" != 0 ]]; then echo '[ERROR] Console log writing failed'; exit 1; fi
exit "${CODES[0]}"
