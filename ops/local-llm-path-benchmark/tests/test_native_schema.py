#!/usr/bin/env python3
"""Offline synthetic/native transport tests. No BLINK credentials or models."""
from __future__ import annotations
import copy
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import compat_gateway as gw
import native_schema as ns
import common
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_support import wait_for_audit_rows
import bench
import process_control as pc
from smoke_checks import evaluate_request


def schema():
    return {'type':'object','properties':{
        'intent_type':{'type':'string','enum':['find_product','knowledge_query','unclear']},
        'texture_preference':{'type':'string','enum':['清爽','不明']},
        'raw_query':{'type':'string'}},
        'required':['intent_type','texture_preference','raw_query'],'additionalProperties':False}


def payload(q='我想了解差別。'):
    return {'model':'unchanged-original-alias','instructions':'合成指示，依本輪輸入判斷。',
            'input':[{'role':'user','content':'【本輪待判斷輸入】\n「'+q+'」'}],
            'text':{'format':{'type':'json_schema','name':'intent_parse_result','strict':True,'schema':schema()}},
            'max_output_tokens':400,'temperature':0.1,'reasoning':{'effort':'none'}}


def converted(p=None):return gw.convert(p or payload(),'/v1/responses',schema_mode=ns.MODE)


class NativeConversion(unittest.TestCase):
    def test_native_adds_only_response_format_to_existing_wire(self):
        p=payload(); original=copy.deepcopy(p)
        baseline,_=gw.convert(p,'/v1/responses'); out,meta=converted(p)
        self.assertEqual(p,original)
        self.assertEqual({k:v for k,v in out.items() if k!='response_format'},baseline)
        self.assertEqual(meta['schema'],schema())
    def test_bound_schema_only_changes_input_echo_enum(self):
        out,meta=converted(); sc=copy.deepcopy(out['response_format']['json_schema']['schema'])
        self.assertEqual(sc['properties']['raw_query'].pop('enum'),['我想了解差別。'])
        self.assertEqual(sc,schema());self.assertFalse(meta['classification_labels_pinned'])
    def test_default_does_not_send_native(self):
        out,meta=gw.convert(payload(),'/v1/responses')
        self.assertNotIn('response_format',out);self.assertEqual(meta['schema_mode'],'prompt_only')
    def test_no_classification_pinning(self):
        out,_=converted();self.assertEqual(out['response_format']['json_schema']['schema']['properties']['intent_type'],schema()['properties']['intent_type'])
    def test_query_uses_last_user_not_history(self):
        p=payload('現在問題。');p['input'][:0]=[{'role':'user','content':'【本輪待判斷輸入】\n「歷史問題」'},{'role':'assistant','content':'舊回答'}]
        out,_=converted(p);self.assertEqual(out['response_format']['json_schema']['schema']['properties']['raw_query']['enum'],['現在問題。'])
    def test_query_with_profile_prefix(self):
        p=payload();p['input'][-1]['content']='背景：完全不同文字\n\n'+p['input'][-1]['content'];out,_=converted(p)
        self.assertEqual(out['response_format']['json_schema']['schema']['properties']['raw_query']['enum'],['我想了解差別。'])
    def test_interior_quotes_and_newline_preserved(self):
        q='含「引號」、"英文"\n以及 😀 \u0301。';out,_=converted(payload(q));self.assertEqual(out['response_format']['json_schema']['schema']['properties']['raw_query']['enum'],[q])
    def test_whitespace_inside_echo_preserved(self):
        out,_=converted(payload('  原字串 \n'));self.assertEqual(out['response_format']['json_schema']['schema']['properties']['raw_query']['enum'],['  原字串 \n'])
    def test_missing_marker_refused(self):
        p=payload();p['input']='plain question'
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_duplicate_marker_refused(self):
        with self.assertRaises(gw.Unsupported):converted(payload('含【本輪待判斷輸入】\n「其他」'))
    def test_trailing_prose_refused(self):
        p=payload();p['input'][-1]['content']+='\nextra'
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_unclosed_quotes_refused(self):
        p=payload();p['input'][-1]['content']=p['input'][-1]['content'][:-1]
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_missing_raw_required_refused(self):
        p=payload();p['text']['format']['schema']['required'].remove('raw_query')
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_preexisting_conflicting_enum_not_widened(self):
        p=payload();p['text']['format']['schema']['properties']['raw_query']['enum']=['different']
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_preexisting_valid_constraint_preserved(self):
        p=payload();p['text']['format']['schema']['properties']['raw_query']['minLength']=2
        out,_=converted(p);self.assertEqual(out['response_format']['json_schema']['schema']['properties']['raw_query']['minLength'],2)
    def test_unknown_raw_schema_refused(self):
        p=payload();p['text']['format']['name']='other'
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_non_intent_schema_forwards_native_without_binding(self):
        p=payload();p['text']['format']={'type':'json_schema','name':'witness','schema':{'type':'object'}}
        out,meta=converted(p);self.assertFalse(meta['raw_query_bound']);self.assertEqual(out['response_format']['json_schema']['schema'],{'type':'object'})
    def test_json_object_remains_original_prompt_only(self):
        p={'messages':[{'role':'user','content':'只回 JSON'}],'response_format':{'type':'json_object'}}
        self.assertEqual(gw.convert(p,'/v1/chat/completions'),gw.convert(p,'/v1/chat/completions',schema_mode=ns.MODE))
    def test_plain_text_unchanged(self):
        p={'input':'只回覆 OK'};self.assertEqual(gw.convert(p,'/v1/responses'),gw.convert(p,'/v1/responses',schema_mode=ns.MODE))
    def test_chat_native_equivalent(self):
        p=payload();c={'messages':[{'role':'system','content':p['instructions']},*p['input']],
                     'response_format':{'type':'json_schema','json_schema':p['text']['format']},'max_tokens':400,'temperature':0.1}
        out,_=gw.convert(c,'/v1/chat/completions',schema_mode=ns.MODE);self.assertEqual(out,converted(p)[0])
    def test_unsupported_tools_stay_rejected(self):
        p=payload();p['tools']=[{'type':'web_search'}]
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_late_system_stay_rejected(self):
        p=payload();p['input'].append({'role':'system','content':'late'})
        with self.assertRaises(gw.Unsupported):converted(p)
    def test_metadata_has_no_raw_value_outside_private_schema(self):
        out,meta=converted(payload('UNIQUE_PRIVATE_MEMBER_TEXT'))
        for k in ('schema','_native_schema'):meta.pop(k,None)
        self.assertNotIn('UNIQUE_PRIVATE_MEMBER_TEXT',json.dumps(meta));self.assertNotIn('messages',meta)
    def test_unknown_mode_refused(self):
        with self.assertRaises(gw.Unsupported):gw.convert(payload(),'/v1/responses',schema_mode='guess')
    def test_native_legacy_selection_refused(self):
        c=json.loads((ROOT/'config.json').read_text());c['schema_mode']=ns.MODE
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_vllm_only_config_accepted(self):
        c=json.loads((ROOT/'config.json').read_text());c.update(schema_mode=ns.MODE,providers=[8200]);bench.validate_config(c)
    def test_original_pair_config_accepted(self):bench.validate_config(json.loads((ROOT/'config.json').read_text()))
    def test_unknown_provider_refused(self):
        c=json.loads((ROOT/'config.json').read_text());c.update(providers=[8010],schema_mode=ns.MODE)
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_owned_vllm_scope_supported(self):
        c=json.loads((ROOT/'config.json').read_text());c.update(vllm_mode='owned',providers=[8220],schema_mode=ns.MODE);bench.validate_config(c)


class NativeWire(unittest.TestCase):
    def pair(self):
        out,meta=converted();model={**meta,'provider_port':8200};dep={'role':'proxy','model_http':True,'path':'/v1/chat/completions','port':8200,'native_wire':ns.wire_metadata(json.dumps(out))}
        return out,model,dep
    def test_exact_proxy_payload_passes(self):
        _,m,d=self.pair();self.assertTrue(ns.transport_proof([m],[d])['pass'])
    def test_missing_format_fails(self):
        out,m,d=self.pair();out.pop('response_format');d['native_wire']=ns.wire_metadata(json.dumps(out));self.assertFalse(ns.transport_proof([m],[d])['pass'])
    def test_schema_changed_fails(self):
        out,m,d=self.pair();out['response_format']['json_schema']['schema']['properties']['raw_query']['enum']=['other'];d['native_wire']=ns.wire_metadata(json.dumps(out));self.assertFalse(ns.transport_proof([m],[d])['pass'])
    def test_prompt_changed_fails(self):
        out,m,d=self.pair();out['messages'][-1]['content']='altered';d['native_wire']=ns.wire_metadata(json.dumps(out));self.assertFalse(ns.transport_proof([m],[d])['pass'])
    def test_wrong_provider_fails(self):
        _,m,d=self.pair();d['port']=8000;self.assertFalse(ns.transport_proof([m],[d])['pass'])
    def test_duplicate_attempts_reported(self):
        _,m,d=self.pair();proof=ns.transport_proof([m],[d,d]);self.assertFalse(proof['pass']);self.assertEqual(proof['duplicate_attempts'],1)
    def test_two_identical_requests_require_two_wire_observations(self):
        _,m,d=self.pair();self.assertFalse(ns.transport_proof([m,m],[d])['pass']);self.assertTrue(ns.transport_proof([m,m],[d,d])['pass'])
    def test_blank_proxy_request_id_does_not_invent_id(self):
        _,m,d=self.pair();m['request_id']='foo';d['request_id']='';self.assertTrue(ns.transport_proof([m],[d])['pass'])
    def test_no_native_original_mode_not_failed(self):self.assertTrue(ns.transport_proof([{'schema_mode':'prompt_only'}],[])['pass'])
    def test_required_native_missing_trace_refused(self):
        self.assertFalse(ns.transport_proof([],[],require_native=True)['pass'])
    def test_required_native_prompt_only_trace_refused(self):
        self.assertFalse(ns.transport_proof([{'schema_mode':'prompt_only'}],[],require_native=True)['pass'])
    def test_malformed_json_observation_is_unknown(self):self.assertEqual(ns.wire_metadata('{')['status'],'unavailable')
    def test_wire_metadata_contains_no_text(self):
        out,_,_=self.pair();self.assertNotIn('我想了解',json.dumps(ns.wire_metadata(json.dumps(out))))


class RealGateway(unittest.TestCase):
    def exercise(self, content, expected_status=200):
        seen=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw=json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}], 'usage':{'completion_tokens':17}}).encode()
                self.send_response(expected_status);self.send_header('Content-Length',str(len(raw)));self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as td:
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);bridge=gw.Server(('127.0.0.1',0),gw.Handler)
            bridge.schema_mode=ns.MODE;bridge.upstream=f'http://127.0.0.1:{model.server_port}';bridge.provider_port=8200;bridge.proxy_port=model.server_port
            bridge.local_key='fixture';bridge.audit_log=str(Path(td)/'audit.jsonl');bridge.request_timeout=3
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (model,bridge)]
            for t in threads:t.start()
            try:
                code,result,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/responses',payload(),3,{'X-Request-ID':'fixture-native'})
                events=wait_for_audit_rows(bridge.audit_log,request_id='fixture-native')
                return code,result,seen,events
            finally:
                for s in (bridge,model):s.shutdown();s.server_close()
                for t in threads:t.join(2)
    def test_http_valid_json_unchanged(self):
        content=json.dumps({'intent_type':'knowledge_query','texture_preference':'不明','raw_query':'我想了解差別。'},ensure_ascii=False)
        code,result,seen,events=self.exercise(content)
        self.assertEqual(code,200);self.assertEqual(len(seen),1);self.assertEqual(result['output'][0]['content'][0]['text'],content)
        self.assertEqual(events[0]['native_bound_schema_validation'],'pass');self.assertNotIn('_native_schema',events[0]);self.assertNotIn('schema',events[0])
    def test_http_wrong_echo_fails_without_repair(self):
        content=json.dumps({'intent_type':'knowledge_query','texture_preference':'不明','raw_query':'wrong'})
        code,result,seen,events=self.exercise(content);self.assertEqual(events[0]['schema_validation'],'fail');self.assertEqual(events[0]['native_bound_schema_validation'],'fail')
        self.assertEqual(result['output'][0]['content'][0]['text'],content);self.assertEqual(len(seen),1)
    def test_http_markdown_not_stripped(self):
        content='```json\n'+json.dumps({'intent_type':'knowledge_query','texture_preference':'不明','raw_query':'我想了解差別。'},ensure_ascii=False)+'\n```'
        _,result,_,ev=self.exercise(content);self.assertEqual(ev[0]['schema_validation'],'fail');self.assertEqual(result['output'][0]['content'][0]['text'],content)
    def test_http_schema_400_not_retried(self):
        code,result,seen,events=self.exercise('error',400);self.assertEqual(code,400);self.assertEqual(len(seen),1);self.assertNotIn('_native_schema',events[0]);self.assertNotIn('schema',events[0])



class NativeFunctionalGate(unittest.TestCase):
    def fixture(self):
        out,m=converted();m={**m,'provider_port':8200,'request_id':'RID','schema_validation':'pass','native_bound_schema_validation':'pass','status':200,'finish_reason':'stop'}
        d={'event':'dependency_http','request_id':'','role':'proxy','model_http':True,'port':8200,'path':'/v1/chat/completions','status':200,'native_wire':ns.wire_metadata(json.dumps(out))}
        row={'request_id':'RID','status':200,'reply_nonempty':True,'correlated':True,'stage':'dispatch','route':'skin_consult','body_ms':12}
        ev={'stages':[{'request_id':'RID','pipeline':'knowledge','stage':'final_generation_taide','status':'success'}],
            'model_calls':[m],'dependencies':[d], 'asgi':[{'request_id':'RID','path':'/api/chat/message','status':200,'total_ms':11}],
            'blocked':[],'auth_resolution':[],'allowed_subprocesses':[]}
        return row,ev
    def test_pass_requires_raw_schema_and_wire(self):
        row,ev=self.fixture();self.assertTrue(evaluate_request(row,'skin_consult',ev,'合成回答')['full_path_pass'])
    def test_http_200_without_wire_proof_fails(self):
        row,ev=self.fixture();ev['dependencies']=[]
        self.assertIn('native_schema_reached_selected_provider',evaluate_request(row,'skin_consult',ev,'合成回答')['failure_reasons'])
    def test_bound_schema_failure_still_fails(self):
        row,ev=self.fixture();ev['model_calls'][0]['native_bound_schema_validation']='fail'
        self.assertIn('native_bound_schema_verified',evaluate_request(row,'skin_consult',ev,'合成回答')['failure_reasons'])
    def test_missing_native_validation_never_vacuous_pass(self):
        row,ev=self.fixture();del ev['model_calls'][0]['schema_validation']
        self.assertIn('schema_verified_when_requested',evaluate_request(row,'skin_consult',ev,'合成回答')['failure_reasons'])
    def test_truncation_still_fails(self):
        row,ev=self.fixture();ev['model_calls'][0]['finish_reason']='length'
        self.assertIn('no_truncated_generation',evaluate_request(row,'skin_consult',ev,'合成回答')['failure_reasons'])
    def test_fallback_still_fails(self):
        row,ev=self.fixture();self.assertFalse(evaluate_request(row,'skin_consult',ev,'抱歉，系統忙碌中，請稍後再試一次 🙏')['full_path_pass'])


class GuardedProxyHTTP(unittest.TestCase):
    """Real proxy HTTP child and guard; fake upstream, NOT actual LiteLLM/GPU."""
    def trial(self,drop_format=False,alter_messages=False):
        import time, os
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);seen=[]
            content=json.dumps({'intent_type':'knowledge_query','texture_preference':'不明','raw_query':'我想了解差別。'},ensure_ascii=False)
            class Model(BaseHTTPRequestHandler):
                def log_message(self,*a):pass
                def do_POST(self):
                    seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                    raw=json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}],'usage':{'completion_tokens':12}}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);mt=threading.Thread(target=model.serve_forever,daemon=True);mt.start()
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));proxy_port=sock.getsockname()[1]
            fixture=d/'proxy_fixture.py'
            fixture.write_text("""import sys,json,httpx
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{}')
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if sys.argv[3]=='1':body.pop('response_format',None)
  if sys.argv[4]=='1':body['messages'][-1]['content']='changed by synthetic proxy'
  with httpx.Client(trust_env=False) as c:r=c.post(sys.argv[2],json=body,timeout=5)
  raw=r.content
  self.send_response(r.status_code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()
""")
            spec={'kind':'script','entry':str(fixture),'service_argv':[str(proxy_port),f'http://127.0.0.1:{model.server_port}/v1/chat/completions',str(int(drop_format)),str(int(alter_messages))],
                'policy':{'role':'proxy','bind_port':proxy_port,'private_root':str(d),'production_root':str(d/'prod'),'shadow_root':str(d/'shadow'),
                          'audit_log':str(d/'proxy-audit.jsonl'),'local_ports':[model.server_port],'llm_ports':[model.server_port]}}
            spec_path=d/'spec.json';spec_path.write_text(json.dumps(spec));j={};jf=d/'journal.json'
            bridge=gw.Server(('127.0.0.1',0),gw.Handler);bridge.schema_mode=ns.MODE;bridge.upstream=f'http://127.0.0.1:{proxy_port}'
            bridge.provider_port=model.server_port;bridge.proxy_port=proxy_port;bridge.local_key='fixture';bridge.audit_log=str(d/'calls.jsonl');bridge.request_timeout=6
            bt=threading.Thread(target=bridge.serve_forever,daemon=True);bt.start();proc=None
            try:
                proc=pc.start_owned([sys.executable,'-I','-B',str(ROOT/'shadow_bootstrap.py'),str(spec_path)],d,dict(os.environ),d/'child.log',proxy_port,j,jf,'fixture')
                try:common.probe(f'http://127.0.0.1:{proxy_port}/',12,process=proc)
                except Exception:self.fail((d/'child.log').read_text())
                code,result,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/responses',payload(),8,{'X-Request-ID':'fixture-native'})
                events=wait_for_audit_rows(d/'calls.jsonl',request_id='fixture-native')
                deps=wait_for_audit_rows(d/'proxy-audit.jsonl',request_id='',event='dependency_http')
                self.assertEqual(code,200);self.assertEqual(result['output'][0]['content'][0]['text'],content);self.assertEqual(len(seen),1)
                return ns.transport_proof(events,deps),events,deps
            finally:
                if proc:pc.stop_owned(j,jf,'fixture',10);proc.wait(timeout=10)
                for server in (bridge,model):server.shutdown();server.server_close()
                bt.join(2);mt.join(2)
    def test_real_outgoing_proxy_metadata_passes(self):
        proof,events,deps=self.trial();self.assertTrue(proof['pass'],(proof,events,deps));self.assertEqual(proof['matched'],1)
    def test_proxy_drop_format_is_not_hidden_by_valid_model_reply(self):
        proof,events,deps=self.trial(drop_format=True);self.assertFalse(proof['pass']);self.assertEqual(events[0]['schema_validation'],'pass')
    def test_proxy_modify_messages_is_detected(self):
        proof,events,deps=self.trial(alter_messages=True);self.assertFalse(proof['pass'])

if __name__=='__main__':unittest.main(verbosity=2)
