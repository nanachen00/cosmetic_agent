#!/usr/bin/env bash
set -uo pipefail

ROOT="${BLINK_INTENT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${1:-$(cat "$ROOT/.state/last_run_id" 2>/dev/null || true)}"

if [[ -z "$RUN_ID" ]]; then
  echo "Usage: $0 [RUN_ID]" >&2
  exit 2
fi

GATE2_DIR="$ROOT/results/$RUN_ID/gate2"
if [[ ! -d "$GATE2_DIR" ]]; then
  echo "ERROR: missing Gate2 result directory: $GATE2_DIR" >&2
  exit 2
fi

set +e
python3 "$SELF_DIR/strict_gate2_finalize.py" \
  --gate2-dir "$GATE2_DIR" \
  --authoritative
RC=$?
set -e

python3 "$SELF_DIR/failure_report.py" \
  --gate2-dir "$GATE2_DIR" \
  --output "$GATE2_DIR/FAILURE_REPORT.json" >/dev/null || true

echo
echo "Rejudged: $GATE2_DIR"
cat "$GATE2_DIR/GATE_STATUS.json"
exit "$RC"
