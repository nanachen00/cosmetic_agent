#!/usr/bin/env python3
"""Fail-closed Gate2 strict finalizer hotfix v2.

Key property:
- Missing internal-full-path-shadow-ab/GATE_STATUS.json never raises FileNotFoundError.
- It first resolves an execution using the root status, LATEST_EXECUTION, or a unique execution dir.
- It may fall back to execution/GATE_STATUS.json.
- Missing/ambiguous/incomplete evidence produces BLOCKED + diagnostics, never a crash and never PASS.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

ARMS = ("A", "B", "C")


def read_json(path: Path, errors: list[str], required: bool = False) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            errors.append(f"missing:{path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"invalid_json:{path}:{type(exc).__name__}:{exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"not_object:{path}")
        return None
    return value


def all_true_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(v is True for v in value.values())


def _candidate_from_pointer(internal: Path, raw: str) -> Path | None:
    raw = raw.strip()
    if not raw:
        return None
    name = Path(raw).name
    candidate = internal / "executions" / name
    return candidate if candidate.is_dir() else None


def resolve_execution(
    internal: Path,
    root_internal_status: dict[str, Any] | None,
    errors: list[str],
) -> tuple[Path | None, dict[str, Any]]:
    resolution: dict[str, Any] = {
        "method": None,
        "candidates": [],
        "complete_candidates": [],
    }

    if isinstance(root_internal_status, dict):
        execution_id = root_internal_status.get("execution")
        if isinstance(execution_id, str):
            candidate = _candidate_from_pointer(internal, execution_id)
            if candidate is not None:
                resolution["method"] = "internal_root_status"
                return candidate, resolution

    latest = internal / "LATEST_EXECUTION"
    if latest.is_file():
        try:
            candidate = _candidate_from_pointer(internal, latest.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"unreadable:{latest}:{type(exc).__name__}:{exc}")
            candidate = None
        if candidate is not None:
            resolution["method"] = "latest_execution_pointer"
            return candidate, resolution

    executions = internal / "executions"
    dirs = sorted([p for p in executions.glob("*") if p.is_dir()]) if executions.is_dir() else []
    resolution["candidates"] = [p.name for p in dirs]

    complete = [
        p for p in dirs
        if (p / "comparison-summary.json").is_file()
        and (p / "driver-status.json").is_file()
    ]
    resolution["complete_candidates"] = [p.name for p in complete]

    if len(complete) == 1:
        resolution["method"] = "unique_complete_execution"
        return complete[0], resolution
    if len(complete) > 1:
        errors.append(
            "ambiguous_execution:"
            + ",".join(p.name for p in complete)
        )
        return None, resolution

    if len(dirs) == 1:
        # Use the sole execution only to emit a precise BLOCKED diagnosis.
        resolution["method"] = "unique_incomplete_execution"
        return dirs[0], resolution

    if len(dirs) == 0:
        errors.append(f"no_execution_directories:{executions}")
    else:
        errors.append(
            "ambiguous_incomplete_execution:"
            + ",".join(p.name for p in dirs)
        )
    return None, resolution


def bool_from_outer_or_evidence(
    outer: dict[str, Any],
    comparison: dict[str, Any] | None,
    driver: dict[str, Any] | None,
) -> bool:
    if outer.get("formal_service_executed") is True:
        return True
    if not isinstance(comparison, dict) or not isinstance(driver, dict):
        return False
    arms = driver.get("arms")
    all_formal_attempted = (
        isinstance(arms, dict)
        and all(
            isinstance(arms.get(a), dict)
            and arms[a].get("formal_attempted") is True
            for a in ARMS
        )
    )
    return (
        comparison.get("experiment_complete") is True
        and bool(driver.get("finished_at"))
        and all_formal_attempted
    )


def blocked_result(
    gate2_dir: Path,
    runner_exit_code: int | None,
    errors: list[str],
    resolution: dict[str, Any] | None = None,
    execution: Path | None = None,
    outer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "blink-intent-gate2-strict-status-v3",
        "gate": 2,
        "phase": "FULL_PATH_SHADOW_AB_AND_PARSER_ONLY_ABLATION",
        "status": "BLOCKED",
        "mandatory_gate": True,
        "advance_allowed": False,
        "formal_service_executed": bool((outer or {}).get("formal_service_executed")),
        "experiment_complete": False,
        "infrastructure_complete": False,
        "functional_acceptance_pass": False,
        "performance_comparison_eligible": False,
        "runner_exit_code": runner_exit_code,
        "checks": {},
        "per_arm": {},
        "internal_execution_id": execution.name if execution else None,
        "internal_harness_status": None,
        "evidence_resolution": resolution or {},
        "evidence_errors": errors,
        "acceptance_rule": (
            "PASS requires complete unambiguous evidence plus original functional acceptance. "
            "Missing evidence blocks; it never crashes and never advances."
        ),
        "next_instruction": "KEEP_GATE2_AND_REVIEW_INCOMPLETE_OR_INVALID_EVIDENCE",
        "gate2_dir": str(gate2_dir),
    }


def evaluate(gate2_dir: Path, runner_exit_code: int | None = None) -> dict[str, Any]:
    gate2_dir = gate2_dir.resolve()
    errors: list[str] = []

    outer = read_json(gate2_dir / "GATE_STATUS.json", errors) or {}
    internal = gate2_dir / "internal-full-path-shadow-ab"

    if not internal.is_dir():
        errors.append(f"missing_directory:{internal}")
        return blocked_result(gate2_dir, runner_exit_code, errors, outer=outer)

    root_internal_status = read_json(internal / "GATE_STATUS.json", errors, required=False)
    execution, resolution = resolve_execution(internal, root_internal_status, errors)

    if execution is None:
        return blocked_result(
            gate2_dir, runner_exit_code, errors,
            resolution=resolution, outer=outer
        )

    execution_status = read_json(execution / "GATE_STATUS.json", errors, required=False)
    effective_internal = root_internal_status or execution_status or {}

    if root_internal_status is None and execution_status is not None:
        resolution["status_source"] = "execution/GATE_STATUS.json"
    elif root_internal_status is not None:
        resolution["status_source"] = "internal-root/GATE_STATUS.json"
    else:
        resolution["status_source"] = None
        errors.append(
            f"missing_status:neither {internal / 'GATE_STATUS.json'} "
            f"nor {execution / 'GATE_STATUS.json'} exists"
        )

    comparison = read_json(execution / "comparison-summary.json", errors, required=True)
    driver = read_json(execution / "driver-status.json", errors, required=True)

    if comparison is None or driver is None or not effective_internal:
        return blocked_result(
            gate2_dir, runner_exit_code, errors,
            resolution=resolution, execution=execution, outer=outer
        )

    integrity_checks = comparison.get("integrity_checks") or effective_internal.get("integrity_checks") or {}
    functional = comparison.get("functional_outcomes") or effective_internal.get("functional_outcomes") or {}
    arms = comparison.get("arms") or {}
    smoke_audits = (
        effective_internal.get("smoke_request_audits")
        or comparison.get("smoke_request_audits")
        or {}
    )

    checks: dict[str, bool] = {}
    checks["gate1_pass"] = bool((outer.get("checks") or {}).get("gate1_pass", True))
    checks["formal_service_executed"] = bool_from_outer_or_evidence(outer, comparison, driver)
    checks["experiment_complete"] = (
        comparison.get("experiment_complete") is True
        and effective_internal.get("experiment_complete") is True
    )
    checks["all_integrity_checks"] = all_true_dict(integrity_checks)
    checks["driver_errors_empty"] = driver.get("errors") == []
    checks["driver_cleanup_pass"] = driver.get("cleanup_pass") is True

    checks["all_arms_smoke_business_pass"] = functional.get("all_arms_smoke_business_pass") is True
    checks["all_arms_formal_business_pass"] = functional.get("all_arms_formal_business_pass") is True
    checks["all_arms_52_full_path_pass"] = functional.get("all_arms_52_full_path_pass") is True

    per_arm: dict[str, Any] = {}
    for arm in ARMS:
        item = arms.get(arm) if isinstance(arms, dict) else None
        if not isinstance(item, dict):
            per_arm[arm] = {"present": False}
            checks[f"arm_{arm}_52_rows"] = False
            checks[f"arm_{arm}_52_full_path_pass"] = False
            checks[f"arm_{arm}_formal_benchmark_pass"] = False
            continue

        rows = item.get("formal_rows")
        passed = item.get("formal_full_path_pass")
        formal_ok = item.get("formal_benchmark_pass") is True
        per_arm[arm] = {
            "present": True,
            "formal_rows": rows,
            "formal_full_path_pass": passed,
            "formal_benchmark_pass": formal_ok,
            "formal_failure_reasons": item.get("formal_failure_reasons") or {},
        }
        checks[f"arm_{arm}_52_rows"] = rows == 52
        checks[f"arm_{arm}_52_full_path_pass"] = passed == 52
        checks[f"arm_{arm}_formal_benchmark_pass"] = formal_ok

    for arm in ARMS:
        audit = smoke_audits.get(arm) if isinstance(smoke_audits, dict) else None
        checks[f"arm_{arm}_strict_smoke_request_audit"] = (
            isinstance(audit, dict)
            and audit.get("pass") is True
            and audit.get("rows") == 4
            and audit.get("verified") == 4
            and audit.get("blockers") == []
        )

    infra_keys = [
        "gate1_pass",
        "formal_service_executed",
        "experiment_complete",
        "all_integrity_checks",
        "driver_errors_empty",
        "driver_cleanup_pass",
        "arm_A_52_rows",
        "arm_B_52_rows",
        "arm_C_52_rows",
        "arm_A_strict_smoke_request_audit",
        "arm_B_strict_smoke_request_audit",
        "arm_C_strict_smoke_request_audit",
    ]
    functional_keys = [
        "all_arms_smoke_business_pass",
        "all_arms_formal_business_pass",
        "all_arms_52_full_path_pass",
        "arm_A_52_full_path_pass",
        "arm_B_52_full_path_pass",
        "arm_C_52_full_path_pass",
        "arm_A_formal_benchmark_pass",
        "arm_B_formal_benchmark_pass",
        "arm_C_formal_benchmark_pass",
    ]

    infrastructure_complete = all(checks.get(k) is True for k in infra_keys)
    functional_acceptance_pass = all(checks.get(k) is True for k in functional_keys)
    gate_pass = infrastructure_complete and functional_acceptance_pass

    if gate_pass:
        status = "PASS"
        next_instruction = "GATE1_GATE2_COMPLETE_REVIEW_RESULTS"
    elif infrastructure_complete:
        status = "FAIL"
        next_instruction = "KEEP_GATE2_AND_REVIEW_FUNCTIONAL_FAILURE_EVIDENCE"
    else:
        status = "BLOCKED"
        next_instruction = "KEEP_GATE2_AND_REVIEW_INCOMPLETE_OR_INVALID_EVIDENCE"

    return {
        "schema": "blink-intent-gate2-strict-status-v3",
        "gate": 2,
        "phase": "FULL_PATH_SHADOW_AB_AND_PARSER_ONLY_ABLATION",
        "status": status,
        "mandatory_gate": True,
        "advance_allowed": gate_pass,
        "formal_service_executed": checks["formal_service_executed"],
        "experiment_complete": checks["experiment_complete"],
        "infrastructure_complete": infrastructure_complete,
        "functional_acceptance_pass": functional_acceptance_pass,
        "performance_comparison_eligible": comparison.get("performance_comparison_eligible") is True,
        "runner_exit_code": runner_exit_code,
        "checks": checks,
        "per_arm": per_arm,
        "internal_execution_id": execution.name,
        "internal_harness_status": effective_internal.get("status"),
        "evidence_resolution": resolution,
        "evidence_errors": errors,
        "acceptance_rule": (
            "PASS requires complete unambiguous evidence plus original functional acceptance; "
            "experiment_complete alone can never advance Gate2."
        ),
        "next_instruction": next_instruction,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate2-dir", required=True)
    ap.add_argument("--runner-exit-code", type=int)
    ap.add_argument("--authoritative", action="store_true")
    ap.add_argument("--output")
    args = ap.parse_args()

    gate2_dir = Path(args.gate2_dir)
    gate2_dir.mkdir(parents=True, exist_ok=True)
    result = evaluate(gate2_dir, args.runner_exit_code)

    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (gate2_dir / "STRICT_ACCEPTANCE.json").write_text(text, encoding="utf-8")

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")

    if args.authoritative:
        authoritative = gate2_dir / "GATE_STATUS.json"
        backup = gate2_dir / "GATE_STATUS.execution-wrapper.json"
        if authoritative.is_file() and not backup.exists():
            shutil.copy2(authoritative, backup)
        authoritative.write_text(text, encoding="utf-8")

    print(text, end="")
    return 0 if result["advance_allowed"] else (8 if result["status"] == "FAIL" else 9)


if __name__ == "__main__":
    raise SystemExit(main())
