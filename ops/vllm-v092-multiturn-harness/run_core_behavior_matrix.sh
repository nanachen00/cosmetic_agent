#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${URL:-http://127.0.0.1:8200}"
VU_LIST="${VU_LIST:-1,2,4,8}"
CONVERSATIONS="${CONVERSATIONS:-10}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/results-core-behavior-$STAMP}"
mkdir -p "$OUT"
OVERALL=0
for SCENARIO in memory_and_constraints structured_json zh_tw_refinement instruction_switching; do
  echo "===== $SCENARIO ====="
  RC=0
  python3 "$HERE/benchmark_multiturn_chat.py" \
    --url "$URL" \
    --scenario "$SCENARIO" \
    --vu-list "$VU_LIST" \
    --turns 4 \
    --conversations-per-vu "$CONVERSATIONS" \
    --output-dir "$OUT/$SCENARIO" \
    --label "CORE-$SCENARIO" || RC=$?
  (( RC > OVERALL )) && OVERALL=$RC
  echo "[SCENARIO_EXIT] $SCENARIO rc=$RC"
done
python3 "$HERE/summarize_matrix.py" --root "$OUT" --output-prefix core_behavior_matrix_summary
echo "[DONE] $OUT overall_rc=$OVERALL"
exit "$OVERALL"
