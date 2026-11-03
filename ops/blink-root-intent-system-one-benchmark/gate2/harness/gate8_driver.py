from __future__ import annotations

import argparse
import copy
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import urllib.request
import uuid

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))

import bench
import profile_backend
import smoke_checks
from auth_clients import load_accounts
from gate8_experiment import Gate8Experiment
from gate8_kev import KevService
from gate8_treatment import (
    TreatmentError, build_wire_result, load_parser, load_policy, parse_kev, selective,
)
from gate8_validate import validate_execution
from gate8_neo4j_routing import merge_exact_hosts
from gate8_admission import classify_smoke_row
import gate2_audit


FIXED_MESSAGES={
    "product_search":"請推薦適合混合肌、無香料、1000 元內的清爽保濕產品。",
    "skin_consult":"玻尿酸與神經醯胺在日常保濕上有什麼差異？請用繁體中文簡短回答。",
    "auto_root":"我想了解玻尿酸和神經醯胺的差別。",
}
KEV_PROBE_MESSAGE="為甚麼油性肌需要注意保濕？"


def atomic(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+".writing")
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)


def install_gate8_validation_adapter():
    """Make reviewed path validation aware of intentional local intent bypass.

    The original harness correctly requires selected-provider/native transport
    whenever the current BLINK path calls the intent model. Gate8 B/C can
    intentionally satisfy that root-intent request locally. In that case only,
    endpoint/native transport is replaced by the explicit local-bypass audit.
    Downstream model calls remain subject to the original :8200 endpoint proof.
    """
    import native_schema

    original_transport=native_schema.transport_proof

    def gate8_transport_proof(models, dependencies, *, require_native=False):
        base=original_transport(models,dependencies,require_native=False)
        local=[
            m for m in models
            if m.get("gate8_intent_bypass") is True
            and m.get("provider_http_called") is False
            and m.get("schema_validation")=="pass"
            and m.get("schema_mode")=="gate8_local_intent_bypass"
        ]
        native_requested=[m for m in models if m.get("native_schema_requested")]
        result=dict(base)
        result.update(
            local_intent_bypass_calls=len(local),
            native_or_local_required=require_native,
            native_requirement_satisfied=bool(native_requested or local or not require_native),
        )
        result["pass"]=bool(base.get("pass")) and result["native_requirement_satisfied"]
        return result

    native_schema.transport_proof=gate8_transport_proof

    original_evaluate=smoke_checks.evaluate_request

    def gate8_evaluate_request(row,scenario,evidence,reply=None):
        mapped="auto_root" if scenario=="kev_probe" else scenario
        result=original_evaluate(row,mapped,evidence,reply)
        result["scenario"]=scenario
        rid=row.get("request_id")
        models=[
            m for m in evidence.get("model_calls",[])
            if rid and m.get("request_id")==rid
        ]
        local=[
            m for m in models
            if m.get("gate8_intent_bypass") is True
            and m.get("provider_http_called") is False
            and m.get("schema_validation")=="pass"
        ]
        if local:
            actual=[m for m in models if not m.get("gate8_intent_bypass")]
            expected_route="product" if mapped=="product_search" else "knowledge"
            local_route_ok=all(m.get("gate8_route")==expected_route for m in local)
            downstream_labels_ok=all(m.get("provider_port")==8200 for m in actual)
            checks=result["checks"]
            # The local intent bypass is itself the selected root-intent path.
            # Any remaining downstream provider calls must still be :8200.
            if "selected_model_endpoint_observed" in checks:
                checks["selected_model_endpoint_observed"]=bool(local_route_ok and downstream_labels_ok)
            if "gateway_provider_label_matches" in checks:
                checks["gateway_provider_label_matches"]=bool(
                    all(m.get("provider_port")==8200 for m in models)
                )
            checks["gate8_local_intent_schema_verified"]=all(
                m.get("schema_validation")=="pass"
                and m.get("response_repaired") is False
                for m in local
            )
            checks["gate8_local_intent_route_matches_scenario"]=local_route_ok
            result["full_path_pass"]=all(checks.values())
            result["failure_reasons"]=[k for k,v in checks.items() if not v]
        return result

    smoke_checks.evaluate_request=gate8_evaluate_request
    bench.evaluate_request=gate8_evaluate_request


def install_kev_probe_alias():
    """Reuse the reviewed auto_root knowledge-path validator for kev_probe."""
    alias=lambda scenario:"auto_root" if scenario=="kev_probe" else scenario

    orig_eval=smoke_checks.evaluate_request
    def evaluate_request(row,scenario,evidence,reply=None):
        result=orig_eval(row,alias(scenario),evidence,reply)
        result["scenario"]=scenario
        return result

    orig_save=smoke_checks.save_smoke_assessment
    def save_smoke_assessment(out,scenario,backend_log=None,wait_seconds=2):
        result=orig_save(out,alias(scenario),backend_log,wait_seconds)
        if isinstance(result,dict):
            result["scenario"]=scenario
            if isinstance(result.get("path"),dict):
                result["path"]["scenario"]=scenario
        return result

    orig_batch=smoke_checks.assess_batch
    def assess_batch(out,scenario,rows,batch_dir,backend_log=None,wait_seconds=2):
        result=orig_batch(out,alias(scenario),rows,batch_dir,backend_log,wait_seconds)
        if isinstance(result,dict):
            result["scenario"]=scenario
            for row in result.get("requests",[]) or []:
                row["scenario"]=scenario
        return result

    smoke_checks.evaluate_request=evaluate_request
    smoke_checks.save_smoke_assessment=save_smoke_assessment
    smoke_checks.assess_batch=assess_batch
    bench.evaluate_request=evaluate_request
    bench.save_smoke_assessment=save_smoke_assessment
    profile_backend.save_smoke_assessment=save_smoke_assessment
    profile_backend.assess_batch=assess_batch


def fixed_scenarios(cfg:dict)->list[dict]:
    current={s["name"]:copy.deepcopy(s) for s in cfg.get("scenarios",[])}
    if set(current)!={"product_search","skin_consult","auto_root"}:
        raise bench.Refuse("Gate8 requires the reviewed three current scenarios")
    out=[]
    for name in ("product_search","skin_consult","auto_root"):
        s=current[name]
        s["message"]=FIXED_MESSAGES[name]
        out.append(s)
    probe=copy.deepcopy(current["auto_root"])
    probe["name"]="kev_probe"
    probe["message"]=KEV_PROBE_MESSAGE
    out.append(probe)
    return out


def phase_cfg(base:dict,scenarios:list[dict],phase:str)->dict:
    cfg=copy.deepcopy(base)
    cfg["providers"]=[8200]
    cfg["scenarios"]=copy.deepcopy(scenarios)
    cfg["auth_check_only"]=phase=="auth"
    cfg["smoke_only"]=phase=="smoke"
    cfg["vus"]=[1] if phase in {"auth","smoke"} else [1,4,8]
    cfg["repetitions"]=1
    return cfg


def safe_scenario(exp:Gate8Experiment,scenario:dict):
    try:
        return exp.scenario(8200,scenario)
    except Exception:
        try:
            if exp.j.get("owned") or getattr(exp,"_open_cell",None):
                exp.release_cell()
        except Exception:
            pass
        raise


def run_phase(exp:Gate8Experiment,arm:str,phase:str,cfg:dict,scenarios:list[dict])->dict:
    pcfg=phase_cfg(cfg,scenarios,phase)
    exp.set_gate8_phase(arm,phase,pcfg)
    if phase!="auth":
        exp.candidate_test()
    attempted=[]
    errors=[]
    for scenario in pcfg["scenarios"]:
        try:
            safe_scenario(exp,scenario)
            attempted.append(scenario["name"])
        except Exception as exc:
            errors.append({
                "scenario":scenario["name"],
                "type":type(exc).__name__,
                "message":bench.redact(str(exc)),
            })
            break
    try:
        summary=bench.aggregate_auth(exp.run) if phase=="auth" else bench.aggregate(exp.run)
    except Exception as exc:
        summary={"benchmark_pass":False,"aggregate_error":type(exc).__name__+":"+bench.redact(str(exc))}
    atomic(exp.run/"gate8-phase-result.json",{
        "gate":8,"arm":arm,"phase":phase,"attempted":attempted,"errors":errors,"summary":summary,
    })
    return {"pass":summary.get("benchmark_pass") is True and not errors,"summary":summary,"errors":errors,"attempted":attempted}


def smoke_experiment_admission(run_dir:Path,phase_result:dict,arm:str)->dict:
    """Gate2 experimental admission with fail-closed request audit.

    Functional FAIL is never upgraded.  The narrow Product pre-treatment
    exception can only continue when the scenario-scoped decision journal has a
    valid chained open/close seal written around the owned gateway lifecycle,
    the request has a model-call witness, and decision/Kev/error counts are all
    exactly zero.  Missing, unfinished, malformed, truncated, or later-mutated
    audit material blocks admission.
    """
    run_dir=Path(run_dir)
    path=run_dir/"path-coverage.csv"
    rows=[]
    if path.is_file():
        with path.open(newline="",encoding="utf-8") as f:
            rows=[r for r in csv.DictReader(f) if r.get("phase")=="smoke"]

    blockers=[]
    outcome_only=[]
    proof_dir=run_dir/"gate2-request-audit-proofs"
    proof_dir.mkdir(parents=True,exist_ok=True)
    all_decisions=[]

    for row in rows:
        rid=str(row.get("request_id") or "")
        proof=gate2_audit.request_proof(run_dir,row,arm)
        if not rid:
            blockers.append({"scenario":row.get("scenario"),"admission_blockers":["missing_request_id"]})
            continue
        proof_path=proof_dir/(hashlib.sha256(rid.encode("utf-8")).hexdigest()+".json")
        atomic(proof_path,proof)
        if proof.get("audit_complete") is not True:
            blockers.append({
                "scenario":row.get("scenario"),"request_id":rid,
                "admission_blockers":["request_audit_incomplete",*("request_audit:"+str(x) for x in proof.get("blockers",[]))],
            })
        # Re-read the sealed cell journal for aggregate treatment invariants.
        try:
            provider=int(row.get("provider_port"))
        except Exception:
            provider=-1
        audit_path=run_dir/f"provider-{provider}"/str(row.get("scenario") or "")/"trace"/"gate8-intent-decisions.jsonl"
        replay=gate2_audit.replay(audit_path,arm=arm,phase="smoke",scenario=str(row.get("scenario") or ""),provider_port=provider)
        if replay.get("complete"):
            all_decisions.extend(x for x in replay.get("events",[]) if x.get("event")=="gate8_intent_decision")

        classification=classify_smoke_row(row,proof)
        if classification["kind"]=="pass":
            continue
        if classification["kind"]=="outcome":
            outcome_only.append(classification)
        else:
            blockers.append(classification)

    if phase_result.get("errors"):
        blockers.append({
            "scenario":"phase_exception",
            "admission_blockers":["phase_exception"],
            "errors":phase_result.get("errors"),
        })

    # Deduplicate aggregate decision rows because each request proof replays a
    # scenario journal that can contain multiple requests in non-smoke modes.
    # Smoke has one member request per scenario, but keying remains explicit.
    seen=set(); decisions=[]
    for d in all_decisions:
        key=(d.get("session_id"),d.get("seq"),d.get("event_sha256"))
        if key in seen: continue
        seen.add(key);decisions.append(d)

    treatment_errors=[d for d in decisions if d.get("error")]
    if treatment_errors:
        blockers.append({
            "scenario":"treatment",
            "admission_blockers":["treatment_error"],
            "error_codes":sorted({str(d.get("error_code") or d.get("error")) for d in treatment_errors}),
            "count":len(treatment_errors),
        })

    parser_bypass=sum(d.get("source")=="parser" and d.get("local_bypass") is True for d in decisions)
    kev_calls=sum(d.get("kev_called") is True for d in decisions)
    kev_bypass=sum(d.get("source")=="kev" and d.get("local_bypass") is True for d in decisions)
    local_bypass=sum(d.get("local_bypass") is True for d in decisions)

    if arm=="A" and (local_bypass or kev_calls):
        blockers.append({"scenario":"treatment","admission_blockers":["A_treatment_not_passthrough"]})
    if arm=="C":
        if kev_calls:
            blockers.append({"scenario":"treatment","admission_blockers":["C_called_kev"]})
        if parser_bypass<1:
            blockers.append({"scenario":"treatment","admission_blockers":["C_parser_bypass_not_exercised"]})
    if arm=="B":
        if parser_bypass<1:
            blockers.append({"scenario":"treatment","admission_blockers":["B_parser_bypass_not_exercised"]})
        if kev_calls<1:
            blockers.append({"scenario":"treatment","admission_blockers":["B_kev_not_exercised"]})
        if kev_bypass<1:
            blockers.append({"scenario":"treatment","admission_blockers":["B_kev_bypass_not_accepted"]})

    if not rows:
        blockers.append({"scenario":"none","admission_blockers":["missing_smoke_rows"]})

    outcome_class_counts={}
    for item in outcome_only:
        name=str(item.get("outcome_class") or "unknown")
        outcome_class_counts[name]=outcome_class_counts.get(name,0)+1

    result={
        "pass":not blockers,
        "functional_pass":bool(phase_result.get("pass")),
        "smoke_rows":len(rows),
        "request_audit_proofs":len(list(proof_dir.glob("*.json"))),
        "request_audit_policy":"complete chained cell audit + request model witness; Product exception additionally requires decision=0, Kev=0, treatment_error=0",
        "allowed_outcome_classes":["generation_truncation","pre_treatment_product_route_deviation"],
        "outcome_only_failures":outcome_only,
        "outcome_class_counts":outcome_class_counts,
        "admission_blockers":blockers,
        "treatment_smoke":{
            "decision_calls":len(decisions),"parser_bypass":parser_bypass,
            "kev_calls":kev_calls,"kev_bypass":kev_bypass,
            "local_bypass":local_bypass,"treatment_errors":len(treatment_errors),
        },
        "policy":(
            "Functional FAIL is preserved. Formal may continue only for generation truncation or "
            "product_search's exact pre-treatment expected_terminal_stage deviation. Product continuation "
            "requires a complete sealed request audit with zero treatment decisions, zero Kev attempts, "
            "zero treatment errors, and every other path check true. Missing/unfinished/truncated audit blocks."
        ),
    }
    atomic(run_dir/"gate8-smoke-admission.json",result)
    return result

def _snapshot_intent_schema(snapshot_source:Path):
    import ast
    path=Path(snapshot_source)/"tools/agent/intent_parser.py"
    if not path.is_file():
        raise RuntimeError("SNAPSHOT_INTENT_PARSER_MISSING")
    raw=path.read_bytes()
    source=raw.decode("utf-8",errors="replace")
    tree=ast.parse(source)
    schema=None
    for node in tree.body:
        targets=node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,ast.AnnAssign) else []
        for target in targets:
            if isinstance(target,ast.Name) and target.id=="INTENT_JSON_SCHEMA":
                try:
                    schema=ast.literal_eval(node.value)
                except Exception:
                    schema=None
    return path,raw,source,tree,schema


def verify_snapshot_intent_contract(snapshot_source:Path,output:Path,tool_root:Path):
    import ast
    path,raw,source,tree,schema=_snapshot_intent_schema(snapshot_source)
    functions={
        n.name for n in ast.walk(tree)
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
    }
    required_functions={
        "_fast_parse_obvious_product_query",
        "_apply_obvious_product_route_guard",
        "_call_openai",
        "parse_intent",
    }
    missing=sorted(required_functions-functions)
    props=(schema or {}).get("properties") or {}
    required=(schema or {}).get("required") or []
    enum=(props.get("intent_type") or {}).get("enum") or []
    checks={
        "required_functions_present":not missing,
        "intent_schema_static_literal":isinstance(schema,dict),
        "raw_query_required":"raw_query" in required and "raw_query" in props,
        "intent_type_required":"intent_type" in required and "intent_type" in props,
        "mappable_enum_present":all(x in enum for x in ("find_product","knowledge_query","unclear")),
        "responses_schema_name_present":'"intent_parse_result"' in source or "'intent_parse_result'" in source,
        "current_query_marker_present":"【本輪待判斷輸入】" in source,
    }

    assembly=[]
    assembly_pass=False
    assembly_error=None
    if all(checks.values()):
        parser=load_parser(Path(tool_root)/"qualified_parser.py")
        cases=[
            ("product",FIXED_MESSAGES["product_search"],"product","product"),
            ("knowledge",FIXED_MESSAGES["auto_root"],"knowledge","knowledge"),
            ("kev_probe",KEV_PROBE_MESSAGE,None,"knowledge"),
        ]
        try:
            for name,query,expected_hint,wire_route in cases:
                parsed=parser.parse(query)
                hint=parser.deterministic_route_hint(parsed)
                if hint!=expected_hint:
                    raise TreatmentError("PREFLIGHT_PARSER_HINT_DRIFT:"+name.upper())
                wire=build_wire_result(wire_route,parsed,query,schema)
                if wire.get("raw_query")!=query:
                    raise TreatmentError("PREFLIGHT_RAW_QUERY_BINDING_MISMATCH")
                assembly.append({
                    "case":name,
                    "query_sha256":hashlib.sha256(query.encode()).hexdigest(),
                    "parser_hint":hint,
                    "wire_route":wire_route,
                    "wire_intent_type":wire.get("intent_type"),
                    "wire_fields":sorted(wire),
                    "pass":True,
                })
            assembly_pass=True
        except Exception as exc:
            code=str(exc) if isinstance(exc,TreatmentError) else type(exc).__name__
            assembly_error=code[:240]
    checks["local_wire_assembly_preflight"]=assembly_pass
    result={
        "pass":all(checks.values()),
        "path":str(path),
        "sha256":hashlib.sha256(raw).hexdigest(),
        "schema_sha256":hashlib.sha256(json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest() if isinstance(schema,dict) else None,
        "checks":checks,
        "missing_functions":missing,
        "intent_enum":enum,
        "required_fields":required,
        "local_wire_assembly_preflight":assembly,
        "assembly_error_code":assembly_error,
        "production_source_mutated":False,
    }
    atomic(output,result)
    if not result["pass"]:
        raise RuntimeError("SNAPSHOT_INTENT_CONTRACT_INCOMPATIBLE:"+str(assembly_error or "STATIC_CONTRACT"))
    return result,schema


def kev_contract_smoke(tool_root:Path,public:Path,schema:dict,port:int=8230)->dict:
    parser=load_parser(tool_root/"qualified_parser.py")
    policy=load_policy(tool_root/"selected_policy.json")
    parsed=parser.parse(KEV_PROBE_MESSAGE)
    hint=parser.deterministic_route_hint(parsed)
    if hint is not None:
        raise RuntimeError("KEV_PROBE_NO_LONGER_PARSER_ABSTAIN")
    questions=json.loads((tool_root/"gate8_questions.json").read_text(encoding="utf-8"))
    payload=json.dumps({"state":KEV_PROBE_MESSAGE,"model":"kev-latest","questions":questions},
                       ensure_ascii=False).encode("utf-8")
    req=urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/systemone",
        data=payload,
        headers={"Content-Type":"application/json"},
        method="POST",
    )
    start=time.perf_counter()
    with urllib.request.urlopen(req,timeout=15) as r:
        body=json.loads(r.read().decode("utf-8"))
        status=r.status
    pred=parse_kev(body)
    route,source=selective(pred,policy)
    wire_schema_pass=False
    wire_error_code=None
    if route is not None:
        try:
            wire=build_wire_result(route,parsed,KEV_PROBE_MESSAGE,schema)
            wire_schema_pass=(wire.get("raw_query")==KEV_PROBE_MESSAGE)
        except Exception as exc:
            wire_error_code=(str(exc) if isinstance(exc,TreatmentError) else type(exc).__name__)[:240]
    result={
        "pass":status==200 and route is not None and wire_schema_pass,
        "query_sha256":hashlib.sha256(KEV_PROBE_MESSAGE.encode()).hexdigest(),
        "parser_hint":hint,
        "kev_pred_route":pred["pred_route"],
        "kev_confidence":pred["confidence"],
        "clarification_probability":pred["clarification_probability"],
        "safety_probability":pred["safety_probability"],
        "selected_route":route,
        "selected_source":source,
        "wire_schema_pass":wire_schema_pass,
        "wire_error_code":wire_error_code,
        "latency_ms":(time.perf_counter()-start)*1000,
        "raw_query_exported":False,
    }
    atomic(public/"kev-smoke.json",result)
    return result


def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--project-root",required=True)
    ap.add_argument("--harness-root",required=True)
    ap.add_argument("--run-id",required=True)
    ap.add_argument("--accounts-file",required=True)
    ap.add_argument("--ack-shared-data-services",action="store_true")
    ap.add_argument("--ack-member-state-memory-writes",action="store_true")
    args=ap.parse_args()
    if not args.ack_shared_data_services:
        raise SystemExit("[REFUSED] --ack-shared-data-services required")
    if not args.ack_member_state_memory_writes:
        raise SystemExit("[REFUSED] --ack-member-state-memory-writes required")

    project=Path(args.project_root).resolve(strict=True)
    live_harness=Path(args.harness_root).resolve(strict=True)
    accounts_file=Path(args.accounts_file).resolve(strict=True)
    gate8_root=project/"results"/args.run_id/"gate2"/"internal-full-path-shadow-ab"
    gate8_root.mkdir(parents=True,exist_ok=True)
    exec_id=time.strftime("%Y%m%d-%H%M%S",time.gmtime())+"-"+uuid.uuid4().hex[:8]
    execution=gate8_root/"executions"/exec_id
    private=project/".state"/args.run_id/"gate2"/exec_id
    execution.mkdir(parents=True,exist_ok=False)
    private.mkdir(parents=True,exist_ok=False,mode=0o700)
    shutil_provenance=HERE/"gate8-install-provenance.json"
    if shutil_provenance.is_file():
        (execution/"gate8-install-provenance.json").write_bytes(shutil_provenance.read_bytes())

    status={
        "gate":8,"execution":exec_id,"started_at":time.time(),"auth_pass":False,
        "arms":{a:{"smoke_attempted":False,"smoke_pass":False,"formal_attempted":False,"formal_pass":False} for a in ("A","B","C")},
        "errors":[],"integration":"model_http_boundary","arm_order":["A","C","B"],
        "primary_formal_per_arm":39,"kev_probe_formal_per_arm":13,
    }
    atomic(execution/"driver-status.json",status)

    project_lock=(project/".gate2.lock")
    harness_lock=(live_harness/".benchmark.lock")
    for p in (project_lock,harness_lock):
        if p.is_symlink():
            raise SystemExit("[ERROR] refusing symlink lock path")
        p.touch(exist_ok=True)
    lock1=project_lock.open("a")
    lock2=harness_lock.open("a")
    try:
        fcntl.flock(lock1,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fcntl.flock(lock2,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("[ERROR] another Gate8/harness workflow is active")

    install_gate8_validation_adapter()
    install_kev_probe_alias()
    cfg=bench.read_json(HERE/"config.json")
    cfg["vllm_mode"]="shared"
    cfg["providers"]=[8200]
    cfg["schema_mode"]="vllm_native_raw_query"
    cfg["intent_output_token_floor"]=None
    cfg["auth_mode"]="member"
    cfg["vus"]=[1,4,8]
    cfg["repetitions"]=1
    cfg["smoke_only"]=False
    cfg["auth_check_only"]=False

    neo4j_routing_policy = merge_exact_hosts(
        cfg, HERE/"gate8_neo4j_routing_hosts.json"
    )
    atomic(execution/"neo4j-routing-policy.json", neo4j_routing_policy)

    primary=fixed_scenarios(cfg)[:3]
    validate_cfg=copy.deepcopy(cfg)
    validate_cfg["scenarios"]=primary
    bench.validate_config(validate_cfg)
    scenarios=fixed_scenarios(cfg)
    cfg["scenarios"]=scenarios

    accounts=load_accounts(accounts_file,required=8)
    cfg["account_aliases"]=[a.alias for a in accounts]
    cfg["account_count"]=len(accounts)

    exp=None
    kev=None
    cleanup_ok=False
    global_error=None
    try:
        atomic(execution/"run-config.json",cfg)
        journal=bench.prepare(cfg,execution,private)
        cfg["snapshot_id"]=journal["snapshot_id"]
        atomic(execution/"run-config.json",cfg)
        snapshot_contract,intent_schema=verify_snapshot_intent_contract(
            private/"snapshot/source",execution/"snapshot-intent-contract.json",HERE
        )
        atomic(execution/"gate8-experiment.json",{
            "gate":8,"execution":exec_id,"snapshot_id":journal["snapshot_id"],
            "arms":{
                "A":"current BLINK intent path",
                "B":"Gate1 qualified Parser + Kev selective + current fallback",
                "C":"Gate1 qualified Parser-only + current fallback",
            },
            "formal":{
                "primary":{"scenarios":["product_search","skin_consult","auto_root"],"vus":[1,4,8],"requests_per_arm":39},
                "kev_probe":{"scenario":"kev_probe","flow_source":"auto_root","vus":[1,4,8],"requests_per_arm":13,
                             "production_prevalence_inference_allowed":False},
            },
            "arm_order":["A","C","B"],
            "same_snapshot_required":True,
            "production_8010_modified":False,
            "shared_8200_modified":False,
            "neo4j_routing_policy":{
                "exact_only":True,
                "port":7687,
                "effective_hosts":list(cfg.get("neo4j_routing_hosts",[])),
                "wildcards_allowed":False,
            },
        })
        exp=Gate8Experiment(cfg,execution,private,journal,accounts=accounts,
                            gate8_root=gate8_root,kev_url="http://127.0.0.1:8230")

        # Auth is arm-independent and explicitly model-inference-free.
        auth=run_phase(exp,"A","auth",cfg,[scenarios[0]])
        status["auth_pass"]=auth["pass"]
        atomic(execution/"driver-status.json",status)
        if not auth["pass"]:
            raise RuntimeError("GATE8_AUTH_PHASE_FAILED")

        for arm in ("A","C","B"):
            if arm=="B":
                kev=KevService(project,project/"results"/args.run_id/"gate1/model-pin.json",
                               private/"kev",execution/"kev",8230)
                kev.start()
                smoke=kev_contract_smoke(HERE,execution/"kev",intent_schema,8230)
                if not smoke["pass"]:
                    raise RuntimeError("GATE8_KEV_CONTRACT_SMOKE_FAILED")

            status["arms"][arm]["smoke_attempted"]=True
            smoke_result=run_phase(exp,arm,"smoke",cfg,scenarios)
            status["arms"][arm]["smoke_pass"]=smoke_result["pass"]
            admission=smoke_experiment_admission(exp.run,smoke_result,arm)
            status["arms"][arm]["smoke_experiment_admission_pass"]=admission["pass"]
            atomic(execution/"driver-status.json",status)

            if admission["pass"]:
                status["arms"][arm]["formal_attempted"]=True
                formal_result=run_phase(exp,arm,"formal",cfg,scenarios)
                status["arms"][arm]["formal_pass"]=formal_result["pass"]
                atomic(execution/"driver-status.json",status)
            else:
                status["arms"][arm]["formal_skip_reason"]="SMOKE_EXPERIMENT_ADMISSION_BLOCKED"
                atomic(execution/"driver-status.json",status)

            if arm=="B" and kev is not None:
                kcleanup=kev.stop()
                kev=None
                if not (kcleanup.get("same_process") and kcleanup.get("stopped") and kcleanup.get("port_released")):
                    raise RuntimeError("GATE8_KEV_CLEANUP_FAILED")

            # Avoid immediate cross-arm burst against shared dependencies.
            if arm!="B":
                time.sleep(max(65,float(cfg.get("batch_cooldown_seconds",65))))

    except Exception as exc:
        global_error={"type":type(exc).__name__,"message":bench.redact(str(exc))}
        status["errors"].append(global_error)
        status["traceback_tail"]=[bench.redact(x) for x in traceback.format_exc().splitlines()[-30:]]
        atomic(execution/"driver-status.json",status)
    finally:
        if kev is not None:
            try:
                kev.stop()
            except Exception as exc:
                status["errors"].append({"type":"KevCleanupError","message":type(exc).__name__})
        if exp is not None:
            try:
                cleanup_ok=exp.cleanup()
            except Exception as exc:
                status["errors"].append({"type":"HarnessCleanupError","message":bench.redact(str(exc))})
                cleanup_ok=False
        status["cleanup_pass"]=cleanup_ok
        status["finished_at"]=time.time()
        atomic(execution/"driver-status.json",status)

    result=validate_execution(execution,gate8_root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print("[EVIDENCE_ARCHIVE]",result.get("evidence_archive"))
    return 0 if result.get("experiment_complete") else 8


if __name__=="__main__":
    raise SystemExit(main())
