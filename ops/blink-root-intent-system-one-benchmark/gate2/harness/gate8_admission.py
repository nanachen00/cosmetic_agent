from __future__ import annotations

import json
from typing import Any

TRUNCATION_REASON = "no_truncated_generation"
PRODUCT_ROUTE_REASON = "expected_terminal_stage"
PRODUCT_ROUTE_CLASS = "pre_treatment_product_route_deviation"


class AdmissionContractError(RuntimeError):
    pass


def _truth(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _parse_reasons(row: dict) -> list[str]:
    try:
        raw = json.loads(row.get("failure_reasons") or "[]")
    except Exception as exc:
        raise AdmissionContractError("UNPARSEABLE_FAILURE_REASONS") from exc
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise AdmissionContractError("INVALID_FAILURE_REASONS")
    return raw


def _parse_checks(row: dict) -> dict:
    try:
        checks = json.loads(row.get("checks") or "{}")
    except Exception as exc:
        raise AdmissionContractError("UNPARSEABLE_CHECKS") from exc
    if not isinstance(checks, dict):
        raise AdmissionContractError("INVALID_CHECKS")
    return checks


def classify_smoke_row(row: dict, audit_proof: dict) -> dict:
    """Classify one Smoke row for experimental admission only.

    This function never mutates or upgrades the functional/full-path verdict.
    """
    if _truth(row.get("full_path_pass")):
        return {
            "kind": "pass",
            "scenario": row.get("scenario"),
            "request_id": row.get("request_id"),
        }

    try:
        reasons = _parse_reasons(row)
        checks = _parse_checks(row)
    except AdmissionContractError as exc:
        return {
            "kind": "blocker",
            "scenario": row.get("scenario"),
            "request_id": row.get("request_id"),
            "failure_reasons": ["admission_contract_error"],
            "admission_blockers": [str(exc)],
        }

    reason_set = set(reasons)
    request_id = str(row.get("request_id") or "")
    audit_proof = audit_proof if isinstance(audit_proof, dict) else {}
    decision_count = int(audit_proof.get("decision_count") or 0)
    kev_attempt_count = int(audit_proof.get("kev_attempt_count") or 0)
    treatment_error_count = int(audit_proof.get("treatment_error_count") or 0)
    base = {
        "scenario": row.get("scenario"),
        "request_id": request_id or None,
        "failure_reasons": sorted(reason_set),
        "treatment_decision_count": decision_count,
        "kev_attempt_count": kev_attempt_count,
        "treatment_error_count": treatment_error_count,
        "request_audit_complete": audit_proof.get("audit_complete") is True,
        "request_audit_proof_sha256": audit_proof.get("proof_sha256"),
    }

    # Existing Gate8 rule: a generation truncation is a measured outcome.
    if reason_set == {TRUNCATION_REASON}:
        return {
            "kind": "outcome",
            "outcome_class": "generation_truncation",
            **base,
        }

    # v9: only this exact Product pre-treatment deviation may continue.
    if reason_set == {PRODUCT_ROUTE_REASON}:
        blockers = []

        if row.get("scenario") != "product_search":
            blockers.append("terminal_stage_failure_not_product_search")
        if row.get("expected_pipeline") != "product":
            blockers.append("product_expected_pipeline_changed")
        if str(row.get("phase")) != "smoke":
            blockers.append("not_smoke_phase")
        if str(row.get("auth_mode")) != "member":
            blockers.append("not_member_smoke")
        if str(row.get("provider_port")) != "8200":
            blockers.append("unexpected_provider")
        if str(row.get("http_status")) != "200":
            blockers.append("product_route_deviation_not_http_200")
        if not request_id:
            blockers.append("missing_request_id")
        if audit_proof.get("audit_complete") is not True:
            blockers.append("request_audit_incomplete")
            blockers.extend("request_audit:" + str(x) for x in (audit_proof.get("blockers") or []))
        if decision_count != 0:
            blockers.append("gate8_treatment_observed_for_product_deviation")
        if kev_attempt_count != 0:
            blockers.append("kev_attempt_observed_for_product_deviation")
        if treatment_error_count != 0:
            blockers.append("treatment_error_observed_for_product_deviation")

        if checks.get("expected_terminal_stage") is not False:
            blockers.append("expected_terminal_stage_not_explicit_false")

        # Future path checks automatically fail closed if they are false.
        false_other_checks = sorted(
            key
            for key, value in checks.items()
            if key != "expected_terminal_stage" and value is not True
        )
        if false_other_checks:
            blockers.append(
                "other_path_checks_failed:" + ",".join(false_other_checks)
            )

        required_true = {
            "http_200_nonempty",
            "request_id_match",
            "asgi_complete_200",
            "response_dispatch_stage",
            "expected_response_route",
            "root_dispatch_observed",
            "no_stage_errors",
            "model_call_observed",
            "no_llm_http_errors",
            "schema_verified_when_requested",
            "no_truncated_generation",
            "no_scenario_policy_blocks",
            "not_known_failure_reply",
            "backend_resolved_same_identity",
            "identity_verified_before_chat",
            "correct_identity_cookie_attached",
            "member_memory_stage_observed",
        }
        missing_required = sorted(required_true - set(checks))
        nontrue_required = sorted(
            key for key in required_true if checks.get(key) is not True
        )
        if missing_required:
            blockers.append(
                "required_path_checks_missing:" + ",".join(missing_required)
            )
        if nontrue_required:
            blockers.append(
                "required_path_checks_not_true:" + ",".join(nontrue_required)
            )

        # Independent CSV-level proof.
        if str(row.get("llm_http_errors")) != "0":
            blockers.append("llm_http_errors_nonzero")
        if str(row.get("schema_validation_failures")) != "0":
            blockers.append("schema_validation_failures_nonzero")
        if str(row.get("policy_block_count")) != "0":
            blockers.append("policy_block_count_nonzero")
        if str(row.get("truncated_generations")) != "0":
            blockers.append("truncated_generations_nonzero")
        if not _truth(row.get("reply_nonempty")):
            blockers.append("reply_empty")

        if not blockers:
            return {
                "kind": "outcome",
                "outcome_class": PRODUCT_ROUTE_CLASS,
                "functional_full_path_pass": False,
                "admission_continue": True,
                "proof": {
                    "only_failure_reason": PRODUCT_ROUTE_REASON,
                    "only_false_path_check": PRODUCT_ROUTE_REASON,
                    "gate8_treatment_decision_count": 0,
                    "kev_attempt_count": 0,
                    "request_audit_complete": True,
                    "request_audit_proof_sha256": audit_proof.get("proof_sha256"),
                    "pre_treatment_inference": True,
                },
                **base,
            }

        return {
            "kind": "blocker",
            "admission_blockers": blockers,
            **base,
        }

    return {
        "kind": "blocker",
        "admission_blockers": sorted(reason_set) or ["unknown_path_failure"],
        **base,
    }
