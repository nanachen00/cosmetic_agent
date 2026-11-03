from __future__ import annotations
import json, tempfile
from pathlib import Path
import gate2_audit

def row(rid='req-1'):
    return {'request_id':rid,'scenario':'product_search','provider_port':'8200','checks':json.dumps({'model_call_observed':True})}

def write_model(base:Path,rid='req-1'):
    p=base/'provider-8200/product_search/trace/model-calls.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({'event':'model_call','request_id':rid})+'\n',encoding='utf-8')
    return p

def make(base:Path):
    p=base/'provider-8200/product_search/trace/gate8-intent-decisions.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    gate2_audit.open_cell(p,session_id='s1',arm='A',phase='smoke',scenario='product_search',provider_port=8200)
    gate2_audit.close_cell(p,session_id='s1',arm='A',phase='smoke',scenario='product_search',provider_port=8200)
    write_model(base)
    return p

cases={}
with tempfile.TemporaryDirectory() as d:
    base=Path(d); p=make(base); proof=gate2_audit.request_proof(base,row(),'A')
    cases['sealed_zero_decision_pass']=proof['audit_complete'] and proof['decision_count']==0 and proof['kev_attempt_count']==0
with tempfile.TemporaryDirectory() as d:
    base=Path(d); p=make(base); raw=p.read_bytes(); p.write_bytes(raw[:-1]); proof=gate2_audit.request_proof(base,row(),'A')
    cases['truncated_final_line_blocks']=proof['audit_complete'] is False
with tempfile.TemporaryDirectory() as d:
    base=Path(d); p=make(base); lines=p.read_text().splitlines(); x=json.loads(lines[0]);x['arm']='B';lines[0]=json.dumps(x,separators=(',',':'));p.write_text('\n'.join(lines)+'\n');proof=gate2_audit.request_proof(base,row(),'A')
    cases['tampered_hash_chain_blocks']=proof['audit_complete'] is False
with tempfile.TemporaryDirectory() as d:
    base=Path(d); write_model(base); proof=gate2_audit.request_proof(base,row(),'A')
    cases['missing_decision_audit_blocks']=proof['audit_complete'] is False
with tempfile.TemporaryDirectory() as d:
    base=Path(d); p=base/'provider-8200/product_search/trace/gate8-intent-decisions.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
    gate2_audit.open_cell(p,session_id='s1',arm='B',phase='smoke',scenario='product_search',provider_port=8200)
    gate2_audit.append_gateway_event(p,session_id='s1',kind='gate8_intent_decision',fields={'request_id':'req-1','kev_called':True,'local_bypass':False,'source':'confidence_fallback','error':None})
    gate2_audit.close_cell(p,session_id='s1',arm='B',phase='smoke',scenario='product_search',provider_port=8200);write_model(base)
    proof=gate2_audit.request_proof(base,row(),'B')
    cases['nonzero_decision_and_kev_counted']=proof['audit_complete'] and proof['decision_count']==1 and proof['kev_attempt_count']==1
if not all(cases.values()): raise SystemExit('[ERROR] '+json.dumps(cases))
print(json.dumps({'pass':True,'cases':cases},indent=2))
