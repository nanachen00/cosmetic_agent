from __future__ import annotations
import argparse, ast, hashlib, inspect, json, os, subprocess, sys
from pathlib import Path
import torch
import kev

EXPECTED = json.loads((Path(__file__).with_name('baseline.json')).read_text(encoding='utf-8'))['gate1']
INTENT_FUNCS={'_fast_parse_obvious_product_query','_apply_obvious_product_route_guard','parse_intent','_call_openai'}
ORCH_FUNCS={'_explicit_skin_consult_intent','_update_intent','_dispatch','_do_dispatch','_handle_find_product','_handle_safety_query'}

def sha(path:Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''): h.update(b)
    return h.hexdigest()

def funcs(path:Path):
    tree=ast.parse(path.read_text(encoding='utf-8',errors='replace'))
    return {n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}

def contract(intent:Path, orch:Path, prod:Path):
    ft=funcs(intent); fo=funcs(orch)
    missing=sorted([f'intent_parser.py:{x}' for x in INTENT_FUNCS-ft]+[f'orchestrator.py:{x}' for x in ORCH_FUNCS-fo])
    text=intent.read_text(encoding='utf-8',errors='replace'); tree=ast.parse(text); found={}
    for node in tree.body:
        targets=node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,ast.AnnAssign) else []
        for t in targets:
            if isinstance(t,ast.Name) and t.id in {'SYSTEM_PROMPT','INTENT_JSON_SCHEMA'}:
                try: found[t.id]=ast.literal_eval(node.value)
                except Exception: pass
    schema=found.get('INTENT_JSON_SCHEMA'); props=(schema or {}).get('properties') or {}; req=(schema or {}).get('required') or []
    enum=(props.get('intent_type') or {}).get('enum') or []
    schema_checks={
      'static_schema_found':isinstance(schema,dict), 'raw_query_property':'raw_query' in props,
      'raw_query_required':'raw_query' in req, 'intent_type_property':'intent_type' in props,
      'intent_type_required':'intent_type' in req, 'find_product':'find_product' in enum,
      'knowledge_query':'knowledge_query' in enum, 'unclear':'unclear' in enum,
    }
    owners=[]
    schema_file=prod/'schemas/intent.py'
    if schema_file.is_file():
        st=ast.parse(schema_file.read_text(encoding='utf-8',errors='replace'))
        for n in ast.walk(st):
            if isinstance(n,ast.ClassDef) and n.name=='IntentParseResult':
                fields=[x.target.id for x in n.body if isinstance(x,ast.AnnAssign) and isinstance(x.target,ast.Name)]
                owners.append({'path':str(schema_file),'fields':fields,'raw_query':'raw_query' in fields,'intent_type':'intent_type' in fields})
    checks={'critical_functions_present':not missing,'intent_schema_mapping':all(schema_checks.values()),'intent_parse_result_present':bool(owners and owners[0]['raw_query'] and owners[0]['intent_type'])}
    return {'status':'PASS' if all(checks.values()) else 'FAIL','critical_missing':missing,'schema_checks':schema_checks,'intent_parse_result_owners':owners,'compatibility_checks':checks,'compatibility_pass':all(checks.values())}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--project-root',required=True); ap.add_argument('--production-root',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    project=Path(a.project_root).resolve(); prod=Path(a.production_root).resolve(); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    adapter=project/'.models'/EXPECTED['adapter_dir_name']
    base_cache=project/'.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B-Base/snapshots'/EXPECTED['base_revision']
    kev_src=project/'vendor/kev'; intent=prod/'tools/agent/intent_parser.py'; orch=prod/'agents/root/orchestrator.py'
    errors=[]
    runtime={'python':sys.version.split()[0],'torch':torch.__version__,'hip':torch.version.hip,'cuda_available':bool(torch.cuda.is_available()),'device':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,'kev_import':str(Path(inspect.getfile(kev)).resolve())}
    if runtime['python']!=EXPECTED['python']: errors.append('python_version_mismatch')
    if runtime['torch']!=EXPECTED['torch']: errors.append('torch_version_mismatch')
    if not runtime['hip']: errors.append('rocm_hip_unavailable')
    if not runtime['cuda_available']: errors.append('torch_rocm_device_unavailable')
    if runtime['device']!=EXPECTED['device']: errors.append('gpu_device_mismatch')
    try:
        commit=subprocess.check_output(['git','-C',str(kev_src),'rev-parse','HEAD'],text=True).strip()
        dirty=subprocess.run(['git','-C',str(kev_src),'status','--porcelain'],capture_output=True,text=True,check=True).stdout.strip()
    except Exception as e:
        commit=None; dirty='git_error:'+type(e).__name__
    if commit!=EXPECTED['kev_source_commit']: errors.append('kev_commit_mismatch')
    if dirty: errors.append('kev_worktree_not_clean')
    try:
        if kev_src.resolve() not in Path(runtime['kev_import']).parents: errors.append('kev_import_not_from_pinned_source')
    except Exception: errors.append('kev_import_path_invalid')
    if not adapter.is_dir(): errors.append('adapter_dir_missing')
    if not base_cache.is_dir(): errors.append('base_cache_missing')
    critical={}
    for name,want in EXPECTED['critical_hashes'].items():
        p=adapter/name
        if not p.is_file(): critical[name]={'present':False,'expected_sha256':want}; errors.append('model_file_missing:'+name); continue
        got=sha(p); critical[name]={'present':True,'sha256':got,'expected_sha256':want,'match':got==want}
        if got!=want: errors.append('model_hash_mismatch:'+name)
    prov={}
    pp=adapter/'provenance.json'
    if pp.is_file(): prov=json.loads(pp.read_text(encoding='utf-8'))
    cfg=prov.get('config') or {}
    if cfg.get('base')!=EXPECTED['base_repo']: errors.append('base_repo_mismatch')
    if cfg.get('base_revision')!=EXPECTED['base_revision']: errors.append('base_revision_mismatch')
    compat=contract(intent,orch,prod) if intent.is_file() and orch.is_file() else {'compatibility_pass':False,'status':'FAIL','critical_missing':['production_source_missing']}
    if not compat.get('compatibility_pass'): errors.append('production_contract_incompatible')
    source_before={str(p):sha(p) for p in (intent,orch) if p.is_file()}
    model_pin={'schema':'gate1-model-pin-v1','adapter_repo':EXPECTED['adapter_repo'],'adapter_revision':EXPECTED['adapter_revision'],'adapter_dir':str(adapter),'base_repo':EXPECTED['base_repo'],'base_revision':EXPECTED['base_revision'],'base_cache':str(base_cache),'resolved_once_then_offline':True,'critical_files':critical}
    result={'pass':not errors,'errors':errors,'runtime':runtime,'kev_commit':commit,'kev_dirty':bool(dirty),'model_pin':model_pin,'production_contract':compat,'source_before':source_before}
    (out/'preflight.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'model-pin.json').write_text(json.dumps(model_pin,ensure_ascii=False,indent=2)+'\n')
    (out/'source-before.json').write_text(json.dumps(source_before,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['pass'] else 3
if __name__=='__main__': raise SystemExit(main())
