from __future__ import annotations

import copy
import json
from gate8_admission import classify_smoke_row

BASE_CHECKS = {
    "http_200_nonempty": True,
    "request_id_match": True,
    "asgi_complete_200": True,
    "response_dispatch_stage": True,
    "expected_response_route": True,
    "expected_terminal_stage": False,
    "root_dispatch_observed": True,
    "no_stage_errors": True,
    "model_call_observed": True,
    "no_llm_http_errors": True,
    "schema_verified_when_requested": True,
    "no_truncated_generation": True,
    "no_scenario_policy_blocks": True,
    "not_known_failure_reply": True,
    "backend_resolved_same_identity": True,
    "identity_verified_before_chat": True,
    "correct_identity_cookie_attached": True,
    "member_memory_stage_observed": True,
}

GOOD_PROOF = {
    "audit_complete": True,
    "decision_count": 0,
    "kev_attempt_count": 0,
    "treatment_error_count": 0,
    "proof_sha256": "selftest-proof",
    "blockers": [],
}

def product_row():
    return {
        "scenario": "product_search",
        "request_id": "shadow-product",
        "phase": "smoke",
        "auth_mode": "member",
        "provider_port": "8200",
        "expected_pipeline": "product",
        "http_status": "200",
        "full_path_pass": "False",
        "failure_reasons": json.dumps(["expected_terminal_stage"]),
        "checks": json.dumps(BASE_CHECKS),
        "llm_http_errors": "0",
        "schema_validation_failures": "0",
        "policy_block_count": "0",
        "truncated_generations": "0",
        "reply_nonempty": "True",
    }

cases = {}

r = classify_smoke_row(product_row(), GOOD_PROOF)
cases["exact_product_deviation_allowed_with_complete_zero_audit"] = (
    r.get("kind") == "outcome"
    and r.get("outcome_class") == "pre_treatment_product_route_deviation"
    and r.get("functional_full_path_pass") is False
)

proof = dict(GOOD_PROOF, decision_count=1)
r = classify_smoke_row(product_row(), proof)
cases["treatment_decision_blocks"] = (
    r.get("kind") == "blocker"
    and "gate8_treatment_observed_for_product_deviation" in r.get("admission_blockers", [])
)

proof = dict(GOOD_PROOF, audit_complete=False, blockers=["audit_not_closed"])
r = classify_smoke_row(product_row(), proof)
cases["incomplete_audit_blocks"] = (
    r.get("kind") == "blocker"
    and "request_audit_incomplete" in r.get("admission_blockers", [])
)

proof = dict(GOOD_PROOF, kev_attempt_count=1)
r = classify_smoke_row(product_row(), proof)
cases["kev_attempt_blocks"] = (
    r.get("kind") == "blocker"
    and "kev_attempt_observed_for_product_deviation" in r.get("admission_blockers", [])
)

bad = product_row()
checks = copy.deepcopy(BASE_CHECKS)
checks["schema_verified_when_requested"] = False
bad["checks"] = json.dumps(checks)
cases["second_false_check_blocks"] = classify_smoke_row(bad, GOOD_PROOF).get("kind") == "blocker"

bad = product_row()
bad["scenario"] = "auto_root"
cases["non_product_terminal_failure_blocks"] = classify_smoke_row(bad, GOOD_PROOF).get("kind") == "blocker"

trunc = product_row()
checks = copy.deepcopy(BASE_CHECKS)
checks["expected_terminal_stage"] = True
checks["no_truncated_generation"] = False
trunc["checks"] = json.dumps(checks)
trunc["failure_reasons"] = json.dumps(["no_truncated_generation"])
r = classify_smoke_row(trunc, GOOD_PROOF)
cases["truncation_still_outcome_only"] = r.get("kind") == "outcome" and r.get("outcome_class") == "generation_truncation"

bad = product_row()
bad["failure_reasons"] = json.dumps(["expected_terminal_stage", "schema_verified_when_requested"])
cases["multiple_reasons_block"] = classify_smoke_row(bad, GOOD_PROOF).get("kind") == "blocker"

if not all(cases.values()):
    raise SystemExit("[ERROR] admission selftest failed: " + json.dumps(cases))

print(json.dumps({"pass": True, "cases": cases}, indent=2))
