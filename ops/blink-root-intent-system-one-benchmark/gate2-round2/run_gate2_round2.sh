#!/usr/bin/env bash
set -uo pipefail

ROOT="${BLINK_INTENT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FINALIZER="$SELF_DIR/strict_gate2_finalize.py"
REPORTER="$SELF_DIR/failure_report.py"

if [[ ! -x "$ROOT/run.sh" ]]; then
  echo "ERROR: missing existing Gate1/Gate2 runner: $ROOT/run.sh" >&2
  exit 2
fi

set +e
"$ROOT/run.sh" gate2 "$@"
RUNNER_RC=$?
set -e

RUN_ID="$(cat "$ROOT/.state/last_run_id" 2>/dev/null || true)"
if [[ -z "$RUN_ID" ]]; then
  echo "ERROR: run completed without .state/last_run_id" >&2
  exit 3
fi

GATE2_DIR="$ROOT/results/$RUN_ID/gate2"
mkdir -p "$GATE2_DIR"

set +e
python3 "$FINALIZER" \
  --gate2-dir "$GATE2_DIR" \
  --runner-exit-code "$RUNNER_RC" \
  --authoritative
STRICT_RC=$?
set -e

python3 "$REPORTER" \
  --gate2-dir "$GATE2_DIR" \
  --output "$GATE2_DIR/FAILURE_REPORT.json" >/dev/null || true

echo
echo "============================================================"
echo "GATE2 ROUND 2 AUTHORITATIVE RESULT"
echo "============================================================"
echo "Run ID: $RUN_ID"
echo "Inner runner exit: $RUNNER_RC"
echo "Result: $GATE2_DIR/GATE_STATUS.json"
echo "Strict details: $GATE2_DIR/STRICT_ACCEPTANCE.json"
echo "Failure report: $GATE2_DIR/FAILURE_REPORT.json"
echo
cat "$GATE2_DIR/GATE_STATUS.json" 2>/dev/null || true

exit "$STRICT_RC"
