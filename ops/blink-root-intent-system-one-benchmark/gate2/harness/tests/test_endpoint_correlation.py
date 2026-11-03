"""Synthetic/loopback regression only; never invokes BLINK, TAIDE, or shared ports."""
from __future__ import annotations
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'tests'))
import common
import compat_gateway as gw
import provider_strategy as ps
import proxy_trace as pt
import smoke_checks as sm
import failure_diagnostics as fd
import comparison_report as cr
import process_control as pc
from test_provider_comparison import minimal_report_run, seed_case, config

CORRELATION = 'gateway_proxy_call_id_v1'
RID = 'shadow-'+'b'*32
CID = 'call-'+'c'*32
IID = 'ingress-'+'d'*32
BODY = {'model':'taide-12b','messages':[{'role':'user','content':'synthetic identical messages'}]}


def fixture(port=8000):
    meta = ps.outgoing_metadata(json.dumps(BODY))
    model = {'request_id':RID,'gateway_call_id':CID,'provider_port':port,'endpoint_request_fingerprint':meta}
    dep = {'role':'proxy','model_http':True,'port':port,'host':'127.0.0.1','path':'/v1/chat/completions',
           'endpoint_wire':meta,'gateway_call_id':CID,'proxy_ingress_id':IID,'proxy_parent_request_id':RID,
           'proxy_context':'active_ingress','proxy_observer':pt.VERSION,'status':200}
    ingress = [{'event':kind,'gateway_call_id':CID,'proxy_ingress_id':IID,'request_id':RID,
                'inference_path':True,'proxy_observer':pt.VERSION}
               for kind in ('proxy_request_started','proxy_request_completed')]
    return [model],[dep],ingress


def proof(m,d,i,port=8000):
    return ps.endpoint_proof(m,d,port,correlation=CORRELATION,ingress=i)


def background(port=8000, same=False):
    _,rows,_=fixture(port);row=rows[0]
    row.update(gateway_call_id=None,proxy_ingress_id=None,proxy_parent_request_id='',proxy_context='no_ingress_context')
    if not same:row['endpoint_wire']=ps.outgoing_metadata(json.dumps({**BODY,'messages':[{'role':'user','content':'different background content'}]}))
    return row


class ExactEvidence(unittest.TestCase):
    def test_unique_call_pass(self):self.assertTrue(proof(*fixture())['pass'])
    def test_native_provider_pass(self):self.assertTrue(proof(*fixture(8200),port=8200)['pass'])
    def test_skin_extra_two_proxy_calls_not_member_failure(self):
        m,d,i=fixture();r=proof(m,d+[background(),background()],i)
        self.assertTrue(r['pass']);self.assertEqual(r['unattributed_proxy_attempts'],2)
        self.assertEqual(r['correlated_outgoing_attempts'],1)
    def test_background_cannot_replace_missing_member_call(self):
        m,d,i=fixture();r=proof(m,[background(same=True)],i)
        self.assertFalse(r['pass']);self.assertEqual(r['missing'],1)
    def test_equal_content_unrelated_call_not_retry(self):
        m,d,i=fixture();r=proof(m,d+[background(same=True)],i)
        self.assertEqual(r['extra_matching_attempts'],0);self.assertEqual(r['unattributed_proxy_attempts'],1)
    def test_background_wrong_port_still_blocks(self):
        m,d,i=fixture();self.assertFalse(proof(m,d+[background(8200)],i)['pass'])
    def test_background_wrong_host_still_blocks(self):
        m,d,i=fixture();b=background();b['host']='outside.invalid';self.assertFalse(proof(m,d+[b],i)['pass'])
    def test_missing_observer_never_degrades_to_hash(self):
        m,d,i=fixture();del d[0]['proxy_observer'];self.assertFalse(proof(m,d,i)['pass'])
    def test_missing_gateway_id_never_degrades_to_hash(self):
        m,d,i=fixture();del m[0]['gateway_call_id'];self.assertFalse(proof(m,d,i)['pass'])
    def test_duplicate_gateway_id_fails(self):
        m,d,i=fixture();self.assertFalse(proof(m*2,d*2,i)['pass'])
    def test_missing_ingress_fails(self):
        m,d,i=fixture();self.assertFalse(proof(m,d,[])['pass'])
    def test_incomplete_proxy_call_waits_not_pass(self):
        m,d,i=fixture();self.assertIn('proxy_completion_missing',proof(m,d,i[:1])['failure_reasons'])
    def test_duplicate_ingress_fails(self):
        m,d,i=fixture();self.assertFalse(proof(m,d,i+i)['pass'])
    def test_orphan_completed_gateway_id_fails(self):
        m,d,i=fixture();d[0]['gateway_call_id']='call-'+'a'*32;self.assertFalse(proof(m,d,i)['pass'])
    def test_orphan_gateway_ingress_fails(self):
        m,d,i=fixture();extra=copy.deepcopy(i[0]);extra['gateway_call_id']='call-'+'e'*32
        self.assertFalse(proof(m,d,i+[extra])['pass'])
    def test_mismatched_ingress_identity_fails(self):
        m,d,i=fixture();i[0]['request_id']='shadow-'+'e'*32;self.assertFalse(proof(m,d,i)['pass'])
    def test_mismatched_outgoing_identity_fails(self):
        m,d,i=fixture();d[0]['proxy_parent_request_id']='shadow-'+'e'*32;self.assertFalse(proof(m,d,i)['pass'])
    def test_mismatched_ingress_id_fails(self):
        m,d,i=fixture();d[0]['proxy_ingress_id']='ingress-'+'e'*32;self.assertFalse(proof(m,d,i)['pass'])
    def test_payload_change_detected_even_same_destination(self):
        m,d,i=fixture();d[0]['endpoint_wire']=background()['endpoint_wire'];self.assertFalse(proof(m,d,i)['pass'])
    def test_changed_retry_not_hidden_by_another_correct_attempt(self):
        m,d,i=fixture();bad=copy.deepcopy(d[0]);bad['endpoint_wire']=background()['endpoint_wire']
        self.assertFalse(proof(m,d+[bad],i)['pass'])
    def test_extra_attempts_visible_not_named_retry(self):
        m,d,i=fixture();r=proof(m,d*2,i);self.assertTrue(r['pass']);self.assertEqual(r['extra_matching_attempts'],1)
    def test_schema_stripping_detected(self):
        m,d,i=fixture();m[0]['endpoint_request_fingerprint']=ps.outgoing_metadata(json.dumps({**BODY,'response_format':{'type':'json_schema','json_schema':{'schema':{'type':'object'}}}}))
        self.assertFalse(proof(m,d,i)['pass'])
    def test_empty_not_vacuous_pass(self):self.assertFalse(proof([],[],[])['pass'])
    def test_wrong_endpoint_path_not_ignored(self):
        m,d,i=fixture();d[0]['path']='/v1/responses';self.assertFalse(proof(m,d,i)['pass'])
    def test_no_observer_background_is_not_excused(self):
        m,d,i=fixture();b=background();b.pop('proxy_observer');self.assertFalse(proof(m,d+[b],i)['pass'])
    def test_unknown_correlation_mode_refused(self):
        with self.assertRaises(common.Refuse):ps.endpoint_proof([],[],8000,correlation='unknown')
    def test_legacy_mode_keeps_previous_strict_definition(self):
        m,d,i=fixture();self.assertFalse(ps.endpoint_proof(m,d+[background()],8000)['pass'])


class ASGIContext(unittest.TestCase):
    def run_async(self, main):return asyncio.run(main())
    def test_concurrent_requests_do_not_share_ids(self):
        async def main():
            seen=[]
            async def app(scope,receive,send):
                a=pt.context_metadata();await asyncio.sleep(.001);b=pt.context_metadata()
                seen.append((a,b));await send({'type':'http.response.start','status':200})
                await send({'type':'http.response.body','body':b'unchanged'})
            async def noop(*args):pass
            async def one(index):
                rid='shadow-'+f'{index:032x}';cid='call-'+f'{index:032x}'
                scope={'type':'http','path':'/v1/chat/completions','headers':[(b'x-request-id',rid.encode()),(pt.HEADER.lower().encode(),cid.encode())]}
                await pt.observe(app,scope,noop,noop,'unused')
            with patch.object(pt,'event'):
                await asyncio.gather(*(one(n) for n in range(8)))
            self.assertEqual(len({a['gateway_call_id'] for a,b in seen}),8)
            self.assertTrue(all(a==b for a,b in seen));self.assertIsNone(pt.context_metadata()['gateway_call_id'])
        self.run_async(main)
    def test_body_and_response_forwarded_without_rewriting(self):
        async def main():
            data={'type':'http.request','body':b'private sentinel'};sent=[]
            async def receive():return data
            async def send(msg):sent.append(msg)
            async def app(scope,receive,send):
                self.assertIs(await receive(),data);await send(data)
            with patch.object(pt,'event') as emit:
                await pt.observe(app,{'type':'http','path':'/v1/chat/completions','headers':[]},receive,send,'unused')
            self.assertIs(sent[0],data);self.assertNotIn('private sentinel',str(emit.call_args_list))
        self.run_async(main)
    def test_exception_does_not_leak_context(self):
        async def main():
            async def app(*a):raise ValueError('synthetic')
            with patch.object(pt,'event'):
                with self.assertRaises(ValueError):await pt.observe(app,{'type':'http','headers':[]},None,None,'unused')
            self.assertIsNone(pt.context_metadata()['gateway_call_id'])
        self.run_async(main)
    def test_lifespan_is_not_changed(self):
        async def main():
            async def app(*a):return 'returned-original'
            with patch.object(pt,'event') as emit:
                self.assertEqual(await pt.observe(app,{'type':'lifespan'},None,None,'unused'),'returned-original')
                emit.assert_not_called()
        self.run_async(main)
    def test_deferred_tasks_marked_after_ingress(self):
        async def main():
            gate=asyncio.Event();tasks=[];seen=[]
            async def later():await gate.wait();seen.append(pt.context_metadata())
            async def app(*args):tasks.append(asyncio.create_task(later()))
            with patch.object(pt,'event'):
                await pt.observe(app,{'type':'http','headers':[(pt.HEADER.lower().encode(),CID.encode())]},None,None,'unused')
                gate.set();await asyncio.gather(*tasks)
            self.assertEqual(seen[0]['proxy_context'],'after_ingress');self.assertEqual(seen[0]['gateway_call_id'],CID)
        self.run_async(main)
    def test_duplicate_or_invalid_header_not_accepted(self):
        async def main():
            seen=[]
            async def app(*a):seen.append(pt.context_metadata())
            with patch.object(pt,'event'):
                await pt.observe(app,{'type':'http','headers':[(pt.HEADER.lower().encode(),CID.encode())]*2},None,None,'unused')
            self.assertIsNone(seen[0]['gateway_call_id'])
        self.run_async(main)


class Diagnosis(unittest.TestCase):
    def test_two_real_failure_classes_not_unknown(self):
        c=fd.classify(['schema_verified_when_requested','selected_model_endpoint_observed'])
        self.assertIn('MODEL_SCHEMA_CONTRACT_FAILURE',c);self.assertIn('ENDPOINT_EVIDENCE_INCOMPLETE',c)
    def test_wrong_port_not_called_schema_problem(self):
        self.assertIn('ROUTE_DESTINATION_MISMATCH',fd.classify([],endpoint={'pass':False,'wrong_destination':1}))
    def test_unknown_failure_not_completed(self):self.assertEqual(fd.classify(['unseen_failure']),['UNCLASSIFIED_GATE_FAILURE'])
    def test_no_reply_schema_diagnostics_leak(self):
        v=cr.safe_contract_diagnostic({'status':'recorded','raw':{'json_parse':'fail','value':'SECRET','parse_error':{'code':'expecting_value','doc':'SECRET','offset':1}}})
        self.assertNotIn('SECRET',json.dumps(v))
    def test_old_style_complete_claim_rejected_for_endpoint_failure(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));cell,_=seed_case(run)
            (cell/'trace/proxy-audit.jsonl').write_text('')
            data=cr.build_report(run)
            self.assertFalse(data['validation']['experiment_evidence_complete'])
            self.assertEqual(data['status'],'COMPARISON_INCOMPLETE_EVIDENCE')
            self.assertIn('ENDPOINT_EVIDENCE_INCOMPLETE',data['providers'][0]['gate_categories'])
    def test_formal_zero_does_not_hide_smoke_panel(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));data=cr.build_report(run)
            text=cr.render(data)
            self.assertIn('Smoke 與停止原因',text);self.assertIn("$('phase').value='smoke'",text)
    def test_paths_selftest_temp_is_inside_harness(self):
        text=(ROOT/'selftest.sh').read_text()
        self.assertIn('TMPDIR="$SELFTEST_TMP"',text);self.assertIn('$HERE/.maintenance/selftest-',text)
    def test_no_skip_schema_gate(self):
        text=(ROOT/'smoke_checks.py').read_text();self.assertIn("'schema_verified_when_requested': not schema_errors",text)


@unittest.skipUnless(all(importlib.util.find_spec(n) for n in ('fastapi','uvicorn','httpx')), 'Optional local FastAPI loopback dependencies absent')
class RealGuardedASGI(unittest.TestCase):
    def trial(self, alter=False):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            class Model(BaseHTTPRequestHandler):
                def log_message(self,*a):pass
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    raw=json.dumps({'choices':[{'message':{'content':'SYNTHETIC_OK'},'finish_reason':'stop'}],'usage':{'prompt_tokens':4,'completion_tokens':2}}).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);mt=threading.Thread(target=model.serve_forever,daemon=True);mt.start()
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            script=d/'asgi_fixture.py'
            script.write_text('''import sys,asyncio,httpx,uvicorn
from fastapi import FastAPI,Request
app=FastAPI()
@app.on_event('startup')
async def startup():
 async with httpx.AsyncClient(trust_env=False) as c:
  for n in range(2):await c.post(sys.argv[2],json={'model':'taide-12b','messages':[{'role':'user','content':'unassigned synthetic proxy task'}]},timeout=5)
@app.get('/health')
async def health():return {'ok':True}
@app.post('/v1/chat/completions')
async def completion(request:Request):
 body=await request.json()
 if sys.argv[3]=='1':body['messages'][-1]['content']='synthetic altered prompt'
 async with httpx.AsyncClient(trust_env=False) as c:
  r=await c.post(sys.argv[2],json=body,timeout=5)
 return r.json()
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_level='warning',access_log=False)
''')
            spec={'kind':'script','entry':str(script),'service_argv':[str(port),f'http://127.0.0.1:{model.server_port}/v1/chat/completions',str(int(alter))],
                  'policy':{'role':'proxy','bind_port':port,'private_root':str(d),'production_root':str(d/'prod'),'shadow_root':str(d/'shadow'),
                            'audit_log':str(d/'proxy-audit.jsonl'),'local_ports':[model.server_port],'llm_ports':[model.server_port]}}
            common.atomic_json(d/'spec.json',spec)
            bridge=gw.Server(('127.0.0.1',0),gw.Handler);bridge.schema_mode='prompt_only';bridge.upstream=f'http://127.0.0.1:{port}'
            bridge.provider_port=model.server_port;bridge.proxy_port=port;bridge.local_key='synthetic';bridge.audit_log=str(d/'calls.jsonl');bridge.request_timeout=8
            bt=threading.Thread(target=bridge.serve_forever,daemon=True);bt.start();proc=None;j={};jf=d/'journal.json'
            try:
                proc=pc.start_owned([sys.executable,'-I','-B',str(ROOT/'shadow_bootstrap.py'),str(d/'spec.json')],d,dict(os.environ),d/'child.log',port,j,jf,'synthetic-proxy')
                try:common.probe(f'http://127.0.0.1:{port}/health',12,process=proc)
                except Exception:self.fail((d/'child.log').read_text())
                from concurrent.futures import ThreadPoolExecutor
                def call(n):
                    rid='shadow-'+f'{n:032x}'
                    status,body,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/chat/completions',BODY,10,{'X-Request-ID':rid})
                    self.assertEqual(status,200);self.assertEqual(body['choices'][0]['message']['content'],'SYNTHETIC_OK')
                with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(call,range(1,4)))
                deadline=time.monotonic()+3
                while True:
                    m=sm.json_events(d/'calls.jsonl');a=sm.json_events(d/'proxy-audit.jsonl')
                    dep=[x for x in a if x.get('event')=='dependency_http']
                    ing=[x for x in a if x.get('event') in {'proxy_request_started','proxy_request_completed'}]
                    result=proof(m,dep,ing,model.server_port)
                    if len(m)==3 and (result['pass'] or time.monotonic()>=deadline):break
                    if time.monotonic()>=deadline:break
                    time.sleep(.03)
                return result,m,dep
            finally:
                if proc:pc.stop_owned(j,jf,'synthetic-proxy',10);proc.wait(timeout=10)
                for server in (bridge,model):server.shutdown();server.server_close()
                bt.join(2);mt.join(2)
    def test_real_proxy_three_parallel_members_and_two_extra_calls(self):
        r,m,d=self.trial();self.assertTrue(r['pass'],r)
        self.assertEqual(r['gateway_calls'],3);self.assertEqual(r['outgoing_attempts'],5)
        self.assertEqual(r['unattributed_proxy_attempts'],2)
        self.assertEqual(len({x['gateway_call_id'] for x in m}),3)
    def test_real_proxy_content_change_still_fails(self):
        r,m,d=self.trial(alter=True);self.assertFalse(r['pass']);self.assertIn('payload_fingerprint_mismatch',r['failure_reasons'])


if __name__=='__main__':unittest.main(verbosity=2)
