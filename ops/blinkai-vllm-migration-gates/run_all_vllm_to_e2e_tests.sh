#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-/var/lib/jenkins/tomod/ops/vllm-migration-gates/$STAMP}"

mkdir -p "$OUT"
echo "Artifacts: $OUT"

run_step() {
  local name="$1"; shift
  echo
  echo "===== $name ====="
  set +e
  "$@"
  local rc=$?
  set -e
  echo "$rc" > "$OUT/${name}.rc"
  echo "[$name] rc=$rc"
  return 0
}

run_step 01_parity \
  python3 "$HERE/01_chat_template_token_parity.py" \
    --output-dir "$OUT/01_parity"

run_step 02_drift \
  python3 "$HERE/02_serving_behavior_drift_gate.py" \
    --output-dir "$OUT/02_drift"

TEMPLATE="$OUT/02_drift/blue_chat_template.jinja"
AB_ARGS=(
  python3 "$HERE/03_structured_ab_recheck.py"
  --output-dir "$OUT/03_ab"
  --conversations "${AB_CONVERSATIONS:-10}"
  --max-tokens "${AB_MAX_TOKENS:-256}"
)
if [[ -s "$TEMPLATE" ]]; then
  AB_ARGS+=(--green-chat-template-file "$TEMPLATE")
fi
run_step 03_ab "${AB_ARGS[@]}"

run_step 04_gate \
  python3 "$HERE/04_close_direct_model_gate.py" \
    --artifact-root "$OUT"

if [[ "${SKIP_E2E:-0}" == "1" ]]; then
  echo
  echo "===== 05_e2e ====="
  echo "SKIPPED by SKIP_E2E=1"
  echo "99" > "$OUT/05_e2e.rc"
else
  E2E_ARGS=(
    python3 "$HERE/05_trace_8010_e2e.py"
    --output-dir "$OUT/05_e2e"
    --repetitions "${E2E_REPETITIONS:-3}"
  )

  if [[ -n "${BLINK_APP_BODY_FILE:-}" ]]; then
    E2E_ARGS+=(--body-file "$BLINK_APP_BODY_FILE")
  else
    : "${BLINK_USER_ID:?Set BLINK_USER_ID or BLINK_APP_BODY_FILE for Step 5}"
    : "${BLINK_CONVERSATION_ID:?Set BLINK_CONVERSATION_ID or BLINK_APP_BODY_FILE for Step 5}"
    E2E_ARGS+=(--user-id "$BLINK_USER_ID" --conversation-id "$BLINK_CONVERSATION_ID")
    [[ -n "${BLINK_FLOW:-}" ]] && E2E_ARGS+=(--flow "$BLINK_FLOW")
  fi

  [[ -n "${BLINK_COOKIE_JAR:-}" ]] && E2E_ARGS+=(--cookie-jar "$BLINK_COOKIE_JAR")
  [[ -n "${BLINK_AUTH_HEADER:-}" ]] && E2E_ARGS+=(--header "$BLINK_AUTH_HEADER")
  [[ -n "${BLINK_PUBLIC_CHAT_URL:-}" ]] && E2E_ARGS+=(--public-url "$BLINK_PUBLIC_CHAT_URL")
  [[ -n "${BLINK_8010_LOG:-}" ]] && E2E_ARGS+=(--app-log "$BLINK_8010_LOG")
  [[ "${BLINK_INCLUDE_STREAM:-0}" == "1" ]] && E2E_ARGS+=(--include-stream)

  run_step 05_e2e "${E2E_ARGS[@]}"
fi

python3 - "$OUT" <<'PY'
import json, sys
from pathlib import Path
root=Path(sys.argv[1])
steps={}
for n in ("01_parity","02_drift","03_ab","04_gate","05_e2e"):
    p=root/f"{n}.rc"
    steps[n]=int(p.read_text()) if p.is_file() else None
summary={"artifact_root":str(root),"return_codes":steps}
gate=root/"04_direct_model_gate.json"
if gate.is_file():
    summary["direct_model_gate"]=json.loads(gate.read_text())
e2e=root/"05_e2e"/"summary.json"
if e2e.is_file():
    summary["e2e_summary"]=json.loads(e2e.read_text())
(root/"all_steps_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
PY

echo
echo "All artifacts: $OUT"
