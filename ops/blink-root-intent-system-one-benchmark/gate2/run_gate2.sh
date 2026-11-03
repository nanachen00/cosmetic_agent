#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="${PROJECT_ROOT:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark}"
RUN_ID="${RUN_ID:-}"
ACCOUNTS="${ACCOUNTS_FILE:-/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-secrets/accounts.json}"
ACK_SHARED=0; ACK_MEMBER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2;;
    --accounts-file) ACCOUNTS="$2"; shift 2;;
    --ack-shared-data-services) ACK_SHARED=1; shift;;
    --ack-member-state-memory-writes) ACK_MEMBER=1; shift;;
    *) echo "[ERROR] unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$RUN_ID" ]] || { echo "[ERROR] --run-id required" >&2; exit 2; }
[[ $ACK_SHARED -eq 1 ]] || { echo "[REFUSED] --ack-shared-data-services required" >&2; exit 2; }
[[ $ACK_MEMBER -eq 1 ]] || { echo "[REFUSED] --ack-member-state-memory-writes required" >&2; exit 2; }
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"; HARNESS="$HERE/harness"; G1="$ROOT/results/$RUN_ID/gate1"; OUT="$ROOT/results/$RUN_ID/gate2"
mkdir -p "$OUT"
[[ -f "$G1/GATE_STATUS.json" ]] || { echo "[ERROR] Gate1 status missing" >&2; exit 3; }
python3 - "$G1/GATE_STATUS.json" <<'PY'
import json,sys
x=json.load(open(sys.argv[1])); assert x.get('status')=='PASS' and x.get('advance_allowed') is True,x
PY
[[ -f "$G1/qualified_parser.py" && -f "$G1/selected_policy.json" && -f "$G1/model-pin.json" ]] || { echo "[ERROR] Gate1 qualified artifacts missing" >&2; exit 3; }
[[ -f "$ACCOUNTS" ]] || { echo "[ERROR] accounts file missing: $ACCOUNTS" >&2; exit 3; }
cp -f "$G1/qualified_parser.py" "$HARNESS/qualified_parser.py"
cp -f "$G1/selected_policy.json" "$HARNESS/selected_policy.json"
rm -rf "$HARNESS/.maintenance" "$HARNESS/__pycache__" "$HARNESS/tests/__pycache__"
python3 "$HARNESS/seal_manifest.py" > "$OUT/manifest-seal.log"
(cd "$HARNESS" && sha256sum -c MANIFEST.sha256) > "$OUT/manifest-check.log"
export PYTHONDONTWRITEBYTECODE=1
SELFTEST_DIR="$OUT/selftests"; mkdir -p "$SELFTEST_DIR"
python3 "$HARNESS/gate2_audit_selftest.py" > "$SELFTEST_DIR/audit.json"
python3 "$HARNESS/gate8_admission_selftest.py" > "$SELFTEST_DIR/admission.json"
python3 "$HARNESS/gate8_gateway_compat_regression.py" > "$SELFTEST_DIR/gateway-compat.json"
python3 "$HARNESS/gate8_selftest.py" > "$SELFTEST_DIR/treatment.json"
PYTHON_BIN=python3 bash "$HARNESS/selftest.sh" > "$SELFTEST_DIR/harness.log" 2>&1
set +e
python3 "$HARNESS/gate8_driver.py" \
  --project-root "$ROOT" \
  --harness-root "$HARNESS" \
  --run-id "$RUN_ID" \
  --accounts-file "$ACCOUNTS" \
  --ack-shared-data-services \
  --ack-member-state-memory-writes
DRIVER_RC=$?
set -e
python3 - "$ROOT" "$RUN_ID" "$DRIVER_RC" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); run=sys.argv[2]; rc=int(sys.argv[3]); out=root/'results'/run/'gate2'; internal=out/'internal-full-path-shadow-ab'
def load(p):
 try:return json.loads(p.read_text(encoding='utf-8'))
 except Exception:return {}
legacy=load(internal/'GATE_STATUS.json')
checks={
 'gate1_pass':load(root/'results'/run/'gate1/GATE_STATUS.json').get('status')=='PASS',
 'driver_exit_zero':rc==0,
 'formal_experiment_complete':legacy.get('experiment_complete') is True,
 'internal_advance_allowed':legacy.get('advance_allowed') is True,
 'strict_smoke_request_audits':(legacy.get('integrity_checks') or {}).get('all_arms_smoke_request_audits_complete') is True,
}
passed=all(checks.values())
status={
 'gate':2,'phase':'FULL_PATH_SHADOW_AB_AND_PARSER_ONLY_ABLATION','status':'PASS' if passed else 'FAIL',
 'mandatory_gate':True,'advance_allowed':passed,'formal_service_executed':bool(legacy),
 'checks':checks,'functional_outcome_all_pass':legacy.get('functional_outcome_all_pass'),
 'functional_outcomes':legacy.get('functional_outcomes'),
 'performance_comparison_eligible':legacy.get('performance_comparison_eligible'),
 'internal_harness_status':legacy.get('status'),
 'historical_equivalence':{'absorbs':['Gate8 full-path Shadow A/B','Parser-only ablation','v10 strict request-audit remediation'],'acceptance_relaxed':False},
 'product_exception_rule':'Exact product_search expected_terminal_stage outcome-only exception requires sealed complete audit and decision_count=0, kev_attempt_count=0, treatment_error_count=0; missing/truncated/unfinished/tampered evidence blocks.',
 'next_instruction':'GATE1_GATE2_COMPLETE_REVIEW_RESULTS' if passed else 'STOP_AND_REVIEW_GATE2_EVIDENCE',
}
(out/'GATE_STATUS.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(status,ensure_ascii=False,indent=2))
raise SystemExit(0 if passed else 8)
PY
