from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def load(p,default=None):
    try:return json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception:return default

def sha(p):
    h=hashlib.sha256(); p=Path(p)
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',required=True); ap.add_argument('--production-root',required=True); ap.add_argument('--preflight-rc',type=int,required=True); ap.add_argument('--parser-rc',type=int,required=True); ap.add_argument('--smoke-rc',type=int,required=True); ap.add_argument('--qualification-rc',type=int,required=True); ap.add_argument('--cleanup-pass',required=True); a=ap.parse_args()
    out=Path(a.out); prod=Path(a.production_root)
    pre=load(out/'preflight.json',{}) or {}; q=load(out/'qualification/GATE_STATUS.json',{}) or {}; sm=load(out/'direct-smoke.json',{}) or {}
    before=load(out/'source-before.json',{}) or {}
    after={}
    for p in (prod/'tools/agent/intent_parser.py',prod/'agents/root/orchestrator.py'):
        if p.is_file(): after[str(p)]=sha(p)
    unchanged=(before==after and bool(before))
    (out/'source-after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2)+'\n')
    policy_path=out/'qualification/policy-gate7-r8-calibration.json'; policy=load(policy_path,{}) or {}
    if policy.get('selected') is not None:
        (out/'selected_policy.json').write_text(json.dumps({'schema':'gate1-selected-policy-v1','source':'Gate7 Retry8 acceptance retained unchanged','selected':policy['selected']},ensure_ascii=False,indent=2)+'\n')
    qp=Path(__file__).with_name('deterministic_parser.py')
    if qp.is_file(): (out/'qualified_parser.py').write_bytes(qp.read_bytes())
    checks={
      'environment_model_source_preflight':a.preflight_rc==0 and pre.get('pass') is True,
      'parser_contract_preflight':a.parser_rc==0,
      'direct_api_contract':a.smoke_rc==0 and (sm.get('summary') or {}).get('contract_pass') is True,
      'mappability_qualification':a.qualification_rc==0 and q.get('status')=='QUALIFIED_FOR_FULL_SHADOW_AB_IMPLEMENTATION' and q.get('advance_allowed') is True,
      'owned_kev_cleanup':a.cleanup_pass.lower()=='true',
      'production_source_unchanged':unchanged,
      'selected_policy_materialized':(out/'selected_policy.json').is_file(),
      'qualified_parser_materialized':(out/'qualified_parser.py').is_file(),
    }
    passed=all(checks.values())
    status={'gate':1,'phase':'QUALIFICATION','status':'PASS' if passed else 'FAIL','mandatory_gate':True,'advance_allowed':passed,'checks':checks,'historical_equivalence':{'absorbs':['Gate0','Gate1A','Gate1B','Gate2','Gate2A observability only','Gate3','Gate4','Gate5','Gate6','Gate7 Retry8'],'acceptance_changed':False,'effective_quality_baseline':'Gate7 Retry8'},'qualification_status':q.get('status'),'next_instruction':'RUN_GATE2' if passed else 'STOP_AND_REVIEW_GATE1_EVIDENCE'}
    (out/'GATE_STATUS.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(status,ensure_ascii=False,indent=2)); return 0 if passed else 10
if __name__=='__main__': raise SystemExit(main())
