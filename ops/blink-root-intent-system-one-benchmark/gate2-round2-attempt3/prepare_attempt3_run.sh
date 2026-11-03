#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${BLINK_INTENT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
SOURCE_RUN=""
TARGET_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-run) SOURCE_RUN="${2:-}"; shift 2 ;;
    --run-id) TARGET_RUN="${2:-}"; shift 2 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$SOURCE_RUN" ]] || { echo "ERROR: --source-run is required" >&2; exit 2; }
SRC="$ROOT/results/$SOURCE_RUN/gate1"
STATUS="$SRC/GATE_STATUS.json"
[[ -f "$STATUS" ]] || { echo "ERROR: Gate1 status missing: $STATUS" >&2; exit 3; }

python3 - "$STATUS" <<'PY'
import json,sys
p=sys.argv[1]
d=json.load(open(p,encoding='utf-8'))
if d.get('status')!='PASS' or d.get('advance_allowed') is not True:
    print('ERROR: source Gate1 is not PASS / advance_allowed=true',file=sys.stderr)
    print(json.dumps(d,indent=2),file=sys.stderr)
    raise SystemExit(3)
PY

if [[ -z "$TARGET_RUN" ]]; then
  RAND="$(python3 - <<'PY'
import secrets
print(secrets.randbelow(9000000)+1000000)
PY
)"
  TARGET_RUN="$(date '+%Y%m%d-%H%M%S')-$RAND"
fi

DST="$ROOT/results/$TARGET_RUN"
[[ ! -e "$DST" ]] || { echo "ERROR: target run already exists: $DST" >&2; exit 4; }
mkdir -p "$DST"
cp -a "$SRC" "$DST/gate1"

cat > "$DST/GATE1_REUSE_PROVENANCE.json" <<EOF
{
  "schema": "blink-intent-gate1-reuse-v1",
  "purpose": "Gate2 Round2 Attempt3",
  "gate1_source_run_id": "$SOURCE_RUN",
  "gate2_target_run_id": "$TARGET_RUN",
  "reason": "Attempt3 changes only Gate2 shadow-harness provider request-ID propagation; Gate1 qualification inputs and acceptance are unchanged.",
  "gate1_reexecuted": false
}
EOF

printf '%s\n' "$TARGET_RUN" > "$ROOT/.state/attempt3_run_id"

echo "Attempt3 RUN_ID: $TARGET_RUN"
echo "Gate1 copied from: $SOURCE_RUN"
echo "Result root: $DST"
