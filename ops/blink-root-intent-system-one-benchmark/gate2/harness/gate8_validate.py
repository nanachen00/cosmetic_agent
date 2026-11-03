from __future__ import annotations

import argparse
import csv
import collections
import hashlib
import json
import math
import shutil
import statistics
import tarfile
from pathlib import Path
from typing import Any

import gate2_audit


ARMS = ("A", "B", "C")
PRIMARY_SCENARIOS = {"product_search", "skin_consult", "auto_root"}
PROBE_SCENARIOS = {"kev_probe"}


def load_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out=[]
    for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def truth(value) -> bool:
    return value is True or str(value).lower() == "true"


def fnum(value):
    try:
        return float(value)
    except Exception:
        return None


def pct(values: list[float], q: float):
    xs=sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not xs:
        return None
    if len(xs)==1:
        return xs[0]
    h=(len(xs)-1)*q
    lo=int(math.floor(h)); hi=int(math.ceil(h))
    return xs[lo]+(h-lo)*(xs[hi]-xs[lo])


def stats(values: list[float]) -> dict:
    xs=[float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not xs:
        return {"n":0,"mean":None,"p50":None,"p95":None,"max":None}
    return {
        "n":len(xs),
        "mean":sum(xs)/len(xs),
        "p50":pct(xs,.5),
        "p95":pct(xs,.95),
        "max":max(xs),
    }


def scenario_group(name: str) -> str:
    return "probe" if name in PROBE_SCENARIOS else "primary"


def formal_rows(arm_root: Path) -> list[dict]:
    return [r for r in read_csv(arm_root/"formal/path-coverage.csv") if r.get("phase")=="formal"]


def decisions(arm_root: Path) -> list[dict]:
    out=[]
    for p in sorted((arm_root/"formal").glob("provider-*/*/trace/gate8-intent-decisions.jsonl")):
        out.extend(read_jsonl(p))
    return [x for x in out if x.get("event")=="gate8_intent_decision"]


def stage_rows(arm_root: Path) -> list[dict]:
    return read_csv(arm_root/"formal/stage-events.csv")


def model_rows(arm_root: Path) -> list[dict]:
    return load_json(arm_root/"formal/model-calls.json",[]) or []


def batch_wall_seconds(arm_root: Path, scenarios: set[str]) -> float:
    total=0.0
    for p in (arm_root/"formal").glob("provider-*/*/repetition-*/vu-*/summary.json"):
        scenario=p.parents[2].name
        if scenario not in scenarios:
            continue
        obj=load_json(p,{}) or {}
        value=obj.get("wall_seconds")
        if isinstance(value,(int,float)) and value>0:
            total += float(value)
    return total


def metric_block(arm_root: Path, scenarios: set[str]) -> dict:
    rows=[r for r in formal_rows(arm_root) if r.get("scenario") in scenarios]
    rids={r.get("request_id") for r in rows if r.get("request_id")}
    dec=[d for d in decisions(arm_root) if d.get("request_id") in rids]
    stages=[s for s in stage_rows(arm_root) if s.get("request_id") in rids]
    models=[m for m in model_rows(arm_root) if m.get("request_id") in rids]

    body=[fnum(r.get("body_ms")) for r in rows]
    intent=[
        fnum(s.get("duration_ms"))
        for s in stages
        if s.get("pipeline")=="root" and "intent_parse" in str(s.get("stage",""))
    ]
    request_count=len(rows)
    pass_count=sum(truth(r.get("full_path_pass")) for r in rows)
    parser_rids={d.get("request_id") for d in dec if d.get("local_bypass") is True and d.get("source")=="parser"}
    kev_called_rids={d.get("request_id") for d in dec if d.get("kev_called") is True}
    kev_bypass_rids={d.get("request_id") for d in dec if d.get("local_bypass") is True and d.get("source")=="kev"}
    upstream_rids={d.get("request_id") for d in dec if d.get("upstream_called") is True}
    decision_rids={d.get("request_id") for d in dec if d.get("request_id")}
    wall=batch_wall_seconds(arm_root,scenarios)
    per_req_model=[]
    for rid in rids:
        per_req_model.append(sum(1 for m in models if m.get("request_id")==rid))
    return {
        "requests":request_count,
        "full_path_pass":pass_count,
        "full_path_pass_rate":pass_count/request_count if request_count else None,
        "e2e_ms":stats(body),
        "root_intent_parse_ms":stats(intent),
        "full_path_goodput_rps":pass_count/wall if wall else None,
        "intent_decision_requests":len(decision_rids),
        "pre_gateway_no_intent_http_rate":1-(len(decision_rids)/request_count) if request_count else None,
        "parser_bypass_rate":len(parser_rids)/request_count if request_count else None,
        "kev_candidate_rate":len(kev_called_rids)/request_count if request_count else None,
        "kev_accepted_bypass_rate":len(kev_bypass_rids)/request_count if request_count else None,
        "current_intent_fallback_request_rate":len(upstream_rids)/request_count if request_count else None,
        "current_intent_fallback_calls_per_request":sum(1 for d in dec if d.get("upstream_called") is True)/request_count if request_count else None,
        "model_calls_per_request":sum(per_req_model)/request_count if request_count else None,
        "treatment_decision_calls":len(dec),
        "treatment_errors":sum(bool(d.get("error")) for d in dec),
        "treatment_error_codes":dict(collections.Counter(
            str(d.get("error_code") or d.get("error"))
            for d in dec if d.get("error")
        )),
        "kev_calls":sum(bool(d.get("kev_called")) for d in dec),
        "local_bypass_calls":sum(bool(d.get("local_bypass")) for d in dec),
    }


def arm_metrics(execution: Path, arm: str) -> dict:
    root=execution/"arms"/arm
    smoke=load_json(root/"smoke/measurement-summary.json",{}) or {}
    admission=load_json(root/"smoke/gate8-smoke-admission.json",{}) or {}
    formal=load_json(root/"formal/measurement-summary.json",{}) or {}
    formal_cov=formal_rows(root)
    decisions_all=decisions(root)
    perf_rows=read_csv(root/"formal/provider-comparison.csv")
    reason_counter=collections.Counter()
    status_counter=collections.Counter()
    for row in formal_cov:
        status_counter[str(row.get("http_status"))]+=1
        if truth(row.get("full_path_pass")):
            continue
        try:
            reason_counter.update(json.loads(row.get("failure_reasons") or "[]"))
        except Exception:
            reason_counter.update(["unparseable_failure_reasons"])
    return {
        "arm":arm,
        "smoke_benchmark_pass":smoke.get("benchmark_pass") is True,
        "smoke_experiment_admission_pass":admission.get("pass") is True,
        "smoke_outcome_only_failures":len(admission.get("outcome_only_failures") or []),
        "smoke_outcome_class_counts":admission.get("outcome_class_counts") or {},
        "formal_benchmark_pass":formal.get("benchmark_pass") is True,
        "formal_rows":len(formal_cov),
        "formal_full_path_pass":sum(truth(r.get("full_path_pass")) for r in formal_cov),
        "formal_failure_reasons":dict(reason_counter),
        "formal_http_statuses":dict(status_counter),
        "primary":metric_block(root,PRIMARY_SCENARIOS),
        "probe":metric_block(root,PROBE_SCENARIOS),
        "decision_total":len(decisions_all),
        "decision_sources":{
            source:sum(d.get("source")==source for d in decisions_all)
            for source in sorted({str(d.get("source")) for d in decisions_all})
        },
        "performance_eligible":bool(perf_rows) and all(truth(r.get("performance_eligible")) for r in perf_rows),
        "dependency_interference_rows":sum(not truth(r.get("performance_eligible")) for r in perf_rows),
        "measurement_summary":{
            "model_http_errors":formal.get("model_http_errors"),
            "backend_llm_http_errors":formal.get("backend_llm_http_errors"),
            "schema_validation_failures":formal.get("schema_validation_failures"),
            "truncated_generations":formal.get("truncated_generations"),
            "blocked_attempts":len(formal.get("blocked_attempts") or []),
            "auth_cleanup_pass":formal.get("auth_cleanup_pass"),
        },
    }



def validate_smoke_request_audits(execution: Path, arm: str) -> dict:
    base = execution / "arms" / arm / "smoke"
    rows = read_csv(base / "path-coverage.csv")
    proof_dir = base / "gate2-request-audit-proofs"
    blockers = []
    verified = 0
    exact_product_exceptions = 0

    if not rows:
        blockers.append("smoke_path_coverage_missing_or_empty")

    expected_names = set()
    for row in rows:
        rid = str(row.get("request_id") or "")
        if not rid:
            blockers.append("smoke_row_missing_request_id")
            continue
        name = hashlib.sha256(rid.encode("utf-8")).hexdigest() + ".json"
        expected_names.add(name)
        proof_path = proof_dir / name
        stored = load_json(proof_path, None)
        if not isinstance(stored, dict):
            blockers.append(f"request_proof_missing:{rid}")
            continue
        verification = gate2_audit.verify_stored_proof(base, row, arm, stored)
        if verification.get("pass") is not True:
            blockers.append(f"request_proof_mismatch:{rid}")
            continue
        if stored.get("audit_complete") is not True:
            blockers.append(f"request_audit_incomplete:{rid}")
            continue

        reasons = []
        try:
            reasons = json.loads(row.get("failure_reasons") or "[]")
        except Exception:
            blockers.append(f"failure_reasons_unparseable:{rid}")
            continue
        exact_product = (
            row.get("scenario") == "product_search"
            and truth(row.get("full_path_pass")) is False
            and set(reasons) == {"expected_terminal_stage"}
        )
        if exact_product:
            exact_product_exceptions += 1
            if int(stored.get("decision_count") or 0) != 0:
                blockers.append(f"product_exception_nonzero_decisions:{rid}")
            if int(stored.get("kev_attempt_count") or 0) != 0:
                blockers.append(f"product_exception_nonzero_kev_attempts:{rid}")
            if int(stored.get("treatment_error_count") or 0) != 0:
                blockers.append(f"product_exception_nonzero_treatment_errors:{rid}")
        verified += 1

    actual_names = {p.name for p in proof_dir.glob("*.json")} if proof_dir.is_dir() else set()
    if actual_names != expected_names:
        blockers.append("request_proof_set_mismatch")

    return {
        "pass": not blockers and verified == len(rows),
        "rows": len(rows),
        "verified": verified,
        "exact_product_exceptions": exact_product_exceptions,
        "proof_files": len(actual_names),
        "blockers": sorted(set(blockers)),
    }

def concatenate_decisions(execution: Path, export: Path):
    out=export/"gate8-intent-decisions.jsonl"
    with out.open("w",encoding="utf-8") as w:
        for arm in ARMS:
            for phase in ("smoke","formal"):
                for p in sorted((execution/"arms"/arm/phase).glob("provider-*/*/trace/gate8-intent-decisions.jsonl")):
                    for line in p.read_text(encoding="utf-8",errors="replace").splitlines():
                        if line.strip():
                            obj=json.loads(line)
                            obj["export_arm"]=arm
                            obj["export_phase"]=phase
                            # Decision audit never contains raw query; enforce.
                            for forbidden in ("query","raw_query","content","messages","input"):
                                obj.pop(forbidden,None)
                            w.write(json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n")


def copy_if(src: Path, dst: Path):
    if src.is_file():
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dst)


def package(execution: Path, gate8_root: Path, status: dict) -> Path:
    export=execution/"evidence-export"
    if export.exists():
        shutil.rmtree(export)
    export.mkdir(mode=0o700)
    for name in (
        "GATE_STATUS.json","driver-status.json","snapshot-manifest.json","source-drift.json",
        "snapshot-intent-contract.json","gate8-experiment.json",
        "neo4j-routing-policy.json",
        "owned-cleanup-proof.json","live-source-observation.json","deployment-observation.json",
        "comparison-summary.json","comparison-summary.csv","gate8-install-provenance.json",
    ):
        copy_if(execution/name,export/name)
    for name in ("kev-smoke.json","process.json","cleanup.json","models.json"):
        copy_if(execution/"kev"/name,export/"kev"/name)
    for arm in ARMS:
        for phase in ("smoke","formal"):
            base=execution/"arms"/arm/phase
            for name in (
                "measurement-summary.json","provider-comparison.csv","path-coverage.csv",
                "stage-events.csv","model-calls.json","smoke-summary.json",
                "native-schema-transport-proof.json","runtime-observation.json","gate8-arm.json",
                "gate8-smoke-admission.json",
            ):
                copy_if(base/name,export/"arms"/arm/phase/name)
    # Export strict request-level audit proofs and sealed decision journals.
    for arm in ARMS:
        for phase in ("smoke", "formal"):
            base = execution / "arms" / arm / phase
            proof_dir = base / "gate2-request-audit-proofs"
            if proof_dir.is_dir():
                for p in sorted(proof_dir.glob("*.json")):
                    copy_if(p, export / "arms" / arm / phase / "gate2-request-audit-proofs" / p.name)
            for p in sorted(base.glob("provider-*/*/trace/gate8-intent-decisions.jsonl")):
                dst = export / "arms" / arm / phase / p.relative_to(base)
                copy_if(p, dst)

    bindings=[]
    for arm in ARMS:
        for phase in ("auth","smoke","formal"):
            base=execution/"arms"/arm/phase
            for p in sorted(base.glob("provider-*/*/trace/gate8-treatment-binding.json")):
                obj=load_json(p,{}) or {}
                obj["arm"]=arm
                obj["phase"]=phase
                obj["path"]=p.relative_to(execution).as_posix()
                bindings.append(obj)
            for p in sorted(base.glob("provider-*/*/trace/gate8-neo4j-egress-binding.json")):
                obj=load_json(p,{}) or {}
                obj["arm"]=arm
                obj["phase"]=phase
                obj["path"]=p.relative_to(execution).as_posix()
                bindings.append(obj)
    (export/"gate8-treatment-bindings.json").write_text(
        json.dumps(bindings,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"
    )
    concatenate_decisions(execution,export)
    archive=gate8_root/f"gate8-clean-execution-{execution.name}-evidence.tar.gz"
    if archive.exists():
        archive.unlink()
    with tarfile.open(archive,"w:gz") as tf:
        tf.add(export,arcname=f"gate8-clean-execution-{execution.name}")
    return archive


def validate_execution(execution: Path, gate8_root: Path) -> dict:
    driver=load_json(execution/"driver-status.json",{}) or {}
    cleanup=load_json(execution/"owned-cleanup-proof.json",{}) or {}
    kev_cleanup=load_json(execution/"kev/cleanup.json",{}) or {}
    snapshot_contract=load_json(execution/"snapshot-intent-contract.json",{}) or {}
    deployment=load_json(execution/"deployment-observation.json",{}) or {}
    arms={arm:arm_metrics(execution,arm) for arm in ARMS}
    smoke_audits={arm:validate_smoke_request_audits(execution,arm) for arm in ARMS}

    snapshots=set()
    for arm in ARMS:
        for phase in ("smoke","formal"):
            cfg=load_json(execution/"arms"/arm/phase/"run-config.json",{}) or {}
            if cfg.get("snapshot_id"):
                snapshots.add(cfg["snapshot_id"])

    integrity_checks={
        "single_snapshot":len(snapshots)==1,
        "snapshot_intent_contract_pass":snapshot_contract.get("pass") is True,
        "local_wire_assembly_preflight_pass":(snapshot_contract.get("checks") or {}).get("local_wire_assembly_preflight") is True,
        "auth_pass":bool(driver.get("auth_pass")),
        "all_arms_smoke_attempted":all(driver.get("arms",{}).get(a,{}).get("smoke_attempted") is True for a in ARMS),
        "all_arms_smoke_experiment_admission_pass":all(arms[a]["smoke_experiment_admission_pass"] for a in ARMS),
        "all_arms_smoke_request_audits_complete":all(smoke_audits[a]["pass"] for a in ARMS),
        "all_arms_formal_attempted":all(driver.get("arms",{}).get(a,{}).get("formal_attempted") is True for a in ARMS),
        "all_arms_52_formal_requests":all(arms[a]["formal_rows"]==52 for a in ARMS),
        "intent_boundary_observed_all_arms":all(arms[a]["primary"]["intent_decision_requests"]>=1 for a in ARMS),
        "A_no_local_treatment":(
            arms["A"]["primary"]["local_bypass_calls"]==0
            and arms["A"]["probe"]["local_bypass_calls"]==0
            and arms["A"]["primary"]["kev_calls"]==0
            and arms["A"]["probe"]["kev_calls"]==0
        ),
        "C_no_kev":arms["C"]["primary"]["kev_calls"]==0 and arms["C"]["probe"]["kev_calls"]==0,
        "C_parser_bypass_exercised":arms["C"]["primary"]["parser_bypass_rate"] is not None and arms["C"]["primary"]["parser_bypass_rate"]>0,
        "C_no_treatment_errors":arms["C"]["primary"]["treatment_errors"]==0 and arms["C"]["probe"]["treatment_errors"]==0,
        "B_parser_bypass_exercised":arms["B"]["primary"]["parser_bypass_rate"] is not None and arms["B"]["primary"]["parser_bypass_rate"]>0,
        "B_kev_probe_exercised":arms["B"]["probe"]["kev_calls"]>=1,
        "B_kev_probe_accepted":arms["B"]["probe"]["kev_accepted_bypass_rate"] is not None and arms["B"]["probe"]["kev_accepted_bypass_rate"]>0,
        "B_no_treatment_errors":arms["B"]["primary"]["treatment_errors"]==0 and arms["B"]["probe"]["treatment_errors"]==0,
        "owned_cleanup_pass":cleanup.get("pass") is True,
        "no_shared_service_actions":cleanup.get("shared_service_actions")==[],
        "kev_cleanup_pass":(
            kev_cleanup.get("stopped") is True
            and kev_cleanup.get("same_process") is True
            and kev_cleanup.get("signal_sent") is True
            and kev_cleanup.get("port_released") is True
        ),
    }
    experiment_complete=all(integrity_checks.values())

    functional_outcomes={
        "all_arms_smoke_business_pass":all(arms[a]["smoke_benchmark_pass"] for a in ARMS),
        "all_arms_formal_business_pass":all(arms[a]["formal_benchmark_pass"] for a in ARMS),
        "all_arms_52_full_path_pass":all(arms[a]["formal_full_path_pass"]==52 for a in ARMS),
        "full_path_pass_rate":{
            a:(arms[a]["formal_full_path_pass"]/52 if arms[a]["formal_rows"]==52 else None)
            for a in ARMS
        },
    }
    functional_outcome_all_pass=all(
        functional_outcomes[k] for k in (
            "all_arms_smoke_business_pass",
            "all_arms_formal_business_pass",
            "all_arms_52_full_path_pass",
        )
    )

    performance_comparison_eligible=(
        deployment.get("performance_eligible") is True
        and all(arms[a]["performance_eligible"] for a in ARMS)
    )

    comparison={
        "gate":8,
        "execution":execution.name,
        "snapshot_ids":sorted(snapshots),
        "integrity_checks":integrity_checks,
        "experiment_complete":experiment_complete,
        "functional_outcomes":functional_outcomes,
        "functional_outcome_all_pass":functional_outcome_all_pass,
        "performance_comparison_eligible":performance_comparison_eligible,
        "deployment_observation_status":deployment.get("status"),
        "arms":arms,
        "smoke_request_audits":smoke_audits,
        "interpretation":{
            "primary_scope":"39 production-like formal requests per arm; use for primary outcome/current-intent-call comparison",
            "probe_scope":"13 deliberately Parser-abstain formal requests per arm; use only for Kev incremental-value measurement",
            "do_not_mix_probe_into_production_prevalence":True,
            "request_level_failures_are_experimental_outcomes":True,
            "experiment_completion_does_not_require_156_of_156_business_pass":True,
            "performance_claims_require_performance_comparison_eligible":True,
            "faster_treatment_not_required_for_gate_completion":True,
        },
    }
    (execution/"comparison-summary.json").write_text(json.dumps(comparison,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    rows=[]
    for arm in ARMS:
        for group in ("primary","probe"):
            m=arms[arm][group]
            rows.append({
                "arm":arm,"group":group,"requests":m["requests"],
                "full_path_pass_rate":m["full_path_pass_rate"],
                "e2e_p50_ms":m["e2e_ms"]["p50"],"e2e_p95_ms":m["e2e_ms"]["p95"],
                "intent_p50_ms":m["root_intent_parse_ms"]["p50"],"intent_p95_ms":m["root_intent_parse_ms"]["p95"],
                "goodput_rps":m["full_path_goodput_rps"],
                "pre_gateway_no_intent_http_rate":m["pre_gateway_no_intent_http_rate"],
                "parser_bypass_rate":m["parser_bypass_rate"],
                "kev_candidate_rate":m["kev_candidate_rate"],
                "kev_accepted_bypass_rate":m["kev_accepted_bypass_rate"],
                "current_intent_fallback_request_rate":m["current_intent_fallback_request_rate"],
                "model_calls_per_request":m["model_calls_per_request"],
                "treatment_errors":m["treatment_errors"],
            })
    with (execution/"comparison-summary.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader();w.writerows(rows)

    status_name="FULL_PATH_SHADOW_ABLATION_COMPLETE" if experiment_complete else "EXECUTION_BLOCKED_OR_INVALID"
    status={
        "gate":8,
        "phase":"FULL_PATH_ABLATION",
        "status":status_name,
        "execution":execution.name,
        "mandatory_gate":True,
        "advance_allowed":experiment_complete,
        "experiment_complete":experiment_complete,
        "functional_outcome_all_pass":functional_outcome_all_pass,
        "performance_comparison_eligible":performance_comparison_eligible,
        "integrity_checks":integrity_checks,
        "functional_outcomes":functional_outcomes,
        "smoke_request_audits":smoke_audits,
        "primary_formal_requests_per_arm":39,
        "probe_formal_requests_per_arm":13,
        "formal_requests_total":156,
        "next_instruction":"RETURN_GATE8_COMPLETE_EVIDENCE_FOR_REVIEW" if experiment_complete else "KEEP_GATE8_AND_REVIEW_INTEGRITY_FAILURE",
    }
    (execution/"GATE_STATUS.json").write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (gate8_root/"GATE_STATUS.json").write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (gate8_root/"LATEST_EXECUTION").write_text(str(execution)+"\n",encoding="utf-8")
    archive=package(execution,gate8_root,status)
    status["evidence_archive"]=str(archive)
    (execution/"GATE_STATUS.json").write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (gate8_root/"GATE_STATUS.json").write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return status


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--execution",required=True)
    ap.add_argument("--gate8-root",required=True)
    a=ap.parse_args()
    result=validate_execution(Path(a.execution),Path(a.gate8_root))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["experiment_complete"] else 8


if __name__=="__main__":
    raise SystemExit(main())
