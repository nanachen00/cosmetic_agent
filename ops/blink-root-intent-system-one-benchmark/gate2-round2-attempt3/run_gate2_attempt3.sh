#!/usr/bin/env bash
set -uo pipefail

ROOT="${BLINK_INTENT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID=""
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "ERROR: --run-id requires a value" >&2; exit 2; }
      RUN_ID="$2"; ARGS+=("$1" "$2"); shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

[[ -n "$RUN_ID" ]] || { echo "ERROR: Attempt3 requires explicit --run-id" >&2; exit 2; }
[[ -x "$ROOT/run.sh" ]] || { echo "ERROR: missing $ROOT/run.sh" >&2; exit 2; }

G1="$ROOT/results/$RUN_ID/gate1/GATE_STATUS.json"
[[ -f "$G1" ]] || { echo "ERROR: Gate1 evidence missing: $G1" >&2; exit 3; }
python3 - "$G1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
if d.get('status')!='PASS' or d.get('advance_allowed') is not True:
    print('ERROR: Gate1 prerequisite is not PASS',file=sys.stderr)
    raise SystemExit(3)
PY

[[ -f "$SELF_DIR/PROVIDER_EGRESS_ID_PATCH_RESULT.json" ]] || {
  echo "ERROR: Attempt3 patch installation evidence missing. Run install_attempt3_fix.sh first." >&2
  exit 4
}

set +e
"$ROOT/run.sh" gate2 "${ARGS[@]}"
RUNNER_RC=$?
set -e

GATE2_DIR="$ROOT/results/$RUN_ID/gate2"
mkdir -p "$GATE2_DIR"

set +e
python3 "$SELF_DIR/strict_gate2_finalize.py" \
  --gate2-dir "$GATE2_DIR" \
  --runner-exit-code "$RUNNER_RC" \
  --authoritative
STRICT_RC=$?
set -e

python3 "$SELF_DIR/failure_report.py" \
  --gate2-dir "$GATE2_DIR" \
  --output "$GATE2_DIR/FAILURE_REPORT.json" >/dev/null || true

cat > "$GATE2_DIR/ATTEMPT3_PATCH_PROVENANCE.json" <<EOF
{
  "schema": "blink-intent-gate2-attempt3-patch-provenance-v1",
  "run_id": "$RUN_ID",
  "change_scope": "proxy-to-vLLM provider request-ID propagation only",
  "generation_budget_changed": false,
  "acceptance_changed": false,
  "production_team_source_modified": false,
  "inner_runner_exit_code": $RUNNER_RC
}
EOF

echo
echo "============================================================"
echo "GATE2 ROUND2 ATTEMPT3 AUTHORITATIVE RESULT"
echo "============================================================"
echo "RUN_ID: $RUN_ID"
echo "Inner runner RC: $RUNNER_RC"
echo "Result: $GATE2_DIR/GATE_STATUS.json"
echo "Strict: $GATE2_DIR/STRICT_ACCEPTANCE.json"
echo "Failure report: $GATE2_DIR/FAILURE_REPORT.json"
echo
cat "$GATE2_DIR/GATE_STATUS.json" 2>/dev/null || true

exit "$STRICT_RC"
