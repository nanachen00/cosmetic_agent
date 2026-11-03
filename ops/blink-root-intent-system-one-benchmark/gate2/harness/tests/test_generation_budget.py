"""Paired token-budget and content-free diagnostic tests (synthetic/loopback only)."""
from __future__ import annotations
import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import common,bench,compat_gateway as gw,generation_budget as gb
import provider_strategy as ps,comparison_report as cr,comparison_workflow as cw
import smoke_checks as sm
import test_native_schema as native
from test_provider_comparison import config,FakeExperiment,minimal_report_run
from test_legacy_contract import content
from test_endpoint_correlation import fixture,proof
from audit_support import wait_for_audit_rows


def cfg():
    c=config();c.update(legacy_contract_mode='legacy_schema_fields',intent_output_token_floor=1024);return c


def convert(p=None,mode='legacy_schema_fields',floor=1024,path='/v1/responses'):
    return gw.convert(native.payload() if p is None else p,path,schema_mode=mode,intent_output_token_floor=floor)


class Conversion(unittest.TestCase):
    def test_legacy_floor_400_to_1024(self):self.assertEqual(convert()[0]['max_tokens'],1024)
    def test_native_same_floor(self):self.assertEqual(convert(mode='vllm_native_raw_query')[0]['max_tokens'],1024)
    def test_original_caller_retained(self):
        _,m=convert();self.assertEqual(m['requested_max_tokens'],400);self.assertEqual(m['generation_budget']['caller_max_tokens'],400)
    def test_forwarded_limit_distinct(self):
        _,m=convert();self.assertEqual(m['effective_max_tokens'],1024)
    def test_input_immutable(self):
        p=native.payload();before=copy.deepcopy(p);convert(p);self.assertEqual(before,p)
    def test_no_prompt_changes_to_legacy(self):
        p=native.payload();a,_=convert(p);b,_=convert(p,floor=None);self.assertEqual(a['messages'],b['messages'])
    def test_no_prompt_or_native_schema_changes(self):
        p=native.payload();a,_=convert(p,mode='vllm_native_raw_query');b,_=convert(p,mode='vllm_native_raw_query',floor=None)
        self.assertEqual({k:v for k,v in a.items() if k!='max_tokens'},{k:v for k,v in b.items() if k!='max_tokens'})
    def test_sampling_unchanged(self):
        p=native.payload();p.update(top_p=.7,seed=8,stop=['END'],presence_penalty=.1,frequency_penalty=.2)
        a,_=convert(p);b,_=convert(p,floor=None);self.assertEqual({k:v for k,v in a.items() if k!='max_tokens'},{k:v for k,v in b.items() if k!='max_tokens'})
    def test_old_default_unchanged(self):self.assertEqual(convert(floor=None)[0]['max_tokens'],400)
    def test_above_floor_never_lowered(self):
        p=native.payload();p['max_output_tokens']=1536;o,m=convert(p);self.assertEqual(o['max_tokens'],1536);self.assertFalse(m['generation_budget']['changed'])
    def test_exact_floor_not_called_changed(self):
        p=native.payload();p['max_output_tokens']=1024;self.assertFalse(convert(p)[1]['generation_budget']['changed'])
    def test_other_schema_limit_unchanged(self):
        p=native.payload();p['text']['format']['name']='other_schema';p['text']['format']['schema']={'type':'object'}
        p['max_output_tokens']=300;o,m=convert(p);self.assertEqual(o['max_tokens'],300);self.assertFalse(m['generation_budget']['applicable'])
    def test_text_limit_unchanged(self):
        o,m=convert({'input':'synthetic text','max_output_tokens':384});self.assertEqual(o['max_tokens'],384);self.assertFalse(m['generation_budget']['applicable'])
    def test_json_object_unchanged(self):
        o,m=convert({'input':'synthetic object','text':{'format':{'type':'json_object'}},'max_output_tokens':128});self.assertEqual(o['max_tokens'],128)
    def test_missing_intent_limit_refused(self):
        p=native.payload();p.pop('max_output_tokens')
        with self.assertRaises(common.Refuse):convert(p)
    def test_invalid_floors_refused(self):
        for x in (0,True,1024.0,'1024',2048,-1):
            with self.subTest(value=x),self.assertRaises(common.Refuse):convert(floor=x)
    def test_chat_budget_equivalent(self):
        p=native.payload();c={'messages':[{'role':'system','content':p['instructions']},*p['input']],
            'response_format':{'type':'json_schema','json_schema':p['text']['format']},'max_tokens':400,'temperature':.1}
        self.assertEqual(convert(p)[0],convert(c,path='/v1/chat/completions')[0])
    def test_no_private_query_in_budget(self):
        _,m=convert(native.payload('PRIVATE_SENTINEL'));self.assertNotIn('PRIVATE_SENTINEL',json.dumps(m['generation_budget']))
    def test_unbound_intent_refused(self):
        with self.assertRaises(common.Refuse):gb.attach({'max_tokens':400},{'schema_name':'intent_parse_result','output_format_type':'json_schema'},floor=1024)
    def test_applied_budget_metadata_valid(self):self.assertTrue(gb.call_budget_verified(convert()[1],1024))
    def test_missing_budget_metadata_invalid(self):self.assertFalse(gb.call_budget_verified({},1024))
    def test_tampered_budget_invalid(self):
        _,m=convert();m['generation_budget']['effective_max_tokens']=400;self.assertFalse(gb.call_budget_verified(m,1024))
    def test_floating_budget_metadata_rejected(self):
        _,m=convert();m['generation_budget']['effective_max_tokens']=1024.0;self.assertFalse(gb.call_budget_verified(m,1024))
    def test_caller_budget_metadata_mismatch_rejected(self):
        _,m=convert();m['requested_max_tokens']=300;self.assertFalse(gb.call_budget_verified(m,1024))
    def test_wrong_profile_invalid(self):
        _,m=convert();m['generation_budget']['version']='wrong';self.assertFalse(gb.call_budget_verified(m,1024))
    def test_free_text_budget_metadata_valid(self):self.assertTrue(gb.call_budget_verified(convert({'input':'x','max_output_tokens':12})[1],1024))
    def test_wire_fingerprint_records_effective_limit(self):self.assertEqual(convert()[1]['endpoint_request_fingerprint']['output_token_limit'],{'status':'observed','value':1024})


class WireProof(unittest.TestCase):
    def rows(self):
        m,d,i=fixture();out,meta=convert();fp=ps.outgoing_metadata(json.dumps(out));m[0].update(meta)
        m[0]['endpoint_request_fingerprint']=fp;d[0]['endpoint_wire']=copy.deepcopy(fp)
        return m,d,i
    def test_correct_budget_reaches_wire(self):self.assertTrue(proof(*self.rows())['pass'])
    def test_proxy_clamp_fails(self):
        m,d,i=self.rows();d[0]['endpoint_wire']['output_token_limit']['value']=400;r=proof(m,d,i)
        self.assertFalse(r['pass']);self.assertIn('output_budget_wire_mismatch',r['failure_reasons'])
    def test_proxy_drop_fails(self):
        m,d,i=self.rows();d[0]['endpoint_wire'].pop('output_token_limit');self.assertFalse(proof(m,d,i)['pass'])
    def test_correct_attempt_cannot_hide_clamp(self):
        m,d,i=self.rows();bad=copy.deepcopy(d[0]);bad['endpoint_wire']['output_token_limit']['value']=400
        self.assertFalse(proof(m,d+[bad],i)['pass'])
    def test_call_detail_exposes_original_effective_observed(self):
        r=proof(*self.rows())['calls'][0];self.assertEqual((r['caller_max_tokens'],r['effective_max_tokens']),(400,1024))
        self.assertEqual(r['observed_output_token_limits'],[{'status':'observed','value':1024}])
    def test_outgoing_extra_private_fields_not_shared(self):
        m,d,i=self.rows();d[0]['endpoint_wire']['output_token_limit']['secret']='PRIVATE_SENTINEL'
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(proof(m,d,i)))
    def test_worker_honoring_is_not_claimed(self):self.assertFalse(proof(*self.rows())['calls'][0]['provider_internal_limit_verified'])
    def test_wire_alias(self):self.assertEqual(gb.wire_limit({'max_completion_tokens':1024}),{'status':'observed','value':1024})
    def test_conflicting_aliases_not_verified(self):self.assertEqual(gb.wire_limit({'max_tokens':400,'max_completion_tokens':1024})['status'],'invalid_or_conflicting')
    def test_absent_limit_not_invented(self):self.assertEqual(gb.wire_limit({}),{'status':'absent','value':None})


class Diagnostics(unittest.TestCase):
    def test_unicode_error_separately_classified(self):
        d=gw.output_contract_diagnostics(r'{"x":"\uZZZZ"}',{'type':'object'},'json_schema')
        self.assertEqual(d['raw']['parse_error']['code'],'invalid_unicode_escape')
    def test_incomplete_unicode_at_end(self):
        d=gw.output_contract_diagnostics('{"x":"\\u12',{'type':'object'},'json_schema',400,{'completion_tokens':400},'stop')
        self.assertEqual(d['completion_limit_signal'],'stop_reported_at_or_above_limit')
        self.assertEqual(d['lexical_summary']['incomplete_unicode_escape_count'],1)
        self.assertTrue(d['token_count_is_not_proof_of_truncation']);self.assertFalse(d['raw_reply_rewritten'])
    def test_invalid_unicode_not_incomplete(self):
        s=gw.json_lexical_summary(r'{"x":"\uZZZZ"}');self.assertEqual(s['invalid_unicode_escape_count'],1);self.assertEqual(s['incomplete_unicode_escape_count'],0)
    def test_valid_unicode_not_treated_as_error(self):
        s=gw.json_lexical_summary(r'{"x":"\u4e2d"}');self.assertEqual(s['unicode_escape_count'],1);self.assertEqual(s['invalid_unicode_escape_count'],0)
    def test_escaped_backslash_not_counted_unicode(self):self.assertEqual(gw.json_lexical_summary(r'{"x":"\\uZZZZ"}')['unicode_escape_count'],0)
    def test_container_chars_inside_string_ignored(self):self.assertEqual(gw.json_lexical_summary('{"x":"{[}]"}')['unclosed_containers'],0)
    def test_missing_closure_reported(self):self.assertEqual(gw.json_lexical_summary('{"x":[')['unclosed_containers'],2)
    def test_trailing_backslash_reported(self):self.assertTrue(gw.json_lexical_summary('{"x":"abc\\')['trailing_backslash'])
    def test_uses_effective_not_old_limit(self):
        d=gw.output_contract_diagnostics(content(),native.schema(),'json_schema',1024,{'completion_tokens':400},'stop')
        self.assertFalse(d['reported_completion_reached_requested_limit']);self.assertEqual(d['completion_limit_signal'],'below_requested_limit')
    def test_length_reported_retained(self):self.assertEqual(gw.output_contract_diagnostics(content(),native.schema(),'json_schema',1024,{'completion_tokens':500},'length')['completion_limit_signal'],'provider_reported_length')
    def test_usage_missing_unknown(self):self.assertEqual(gw.output_contract_diagnostics(content(),native.schema(),'json_schema')['completion_limit_signal'],'UNKNOWN')
    def test_share_sanitizer_keeps_diagnostic_not_doc(self):
        d=gw.output_contract_diagnostics(r'{"x":"PRIVATE_SENTINEL\uZZZZ"}',{'type':'object'},'json_schema');d['raw']['doc']='PRIVATE_SENTINEL'
        s=cr.safe_contract_diagnostic(d);self.assertNotIn('PRIVATE_SENTINEL',json.dumps(s));self.assertEqual(s['raw']['parse_error']['code'],'invalid_unicode_escape')
    def test_nonstring_diagnostic_signal_does_not_crash(self):
        self.assertEqual(cr.safe_contract_diagnostic({'completion_limit_signal':{'private':'x'}})['completion_limit_signal'],'UNKNOWN')
    def test_budget_sanitizer_drops_private_fields(self):
        b=convert()[1]['generation_budget'];b['secret']='PRIVATE_SENTINEL';self.assertNotIn('PRIVATE_SENTINEL',json.dumps(gb.public_budget(b)))


class Workflow(unittest.TestCase):
    def test_configuration_accepted(self):bench.validate_config(cfg())
    def test_both_providers_have_equal_policy(self):
        a,b=(ps.strategy(cfg(),p) for p in (8000,8200));self.assertEqual(a['intent_output_token_floor'],b['intent_output_token_floor']);self.assertEqual(a['generation_budget_version'],b['generation_budget_version'])
    def test_noncomparison_floor_refused(self):
        c=cfg();c.update(schema_mode='prompt_only',legacy_contract_mode='legacy_prompt_contract')
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_wrong_legacy_profile_refused(self):
        c=cfg();c['legacy_contract_mode']='legacy_explicit_json'
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_config_immutable(self):
        c=cfg();before=copy.deepcopy(c);ps.strategy(c,8000);ps.strategy(c,8200);self.assertEqual(c,before)
    def test_smoke_failure_records_legacy_outcome_and_continues_8200(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td))
            def aggregate(_):
                return {'benchmark_pass':e.cfg['providers'][0]!=8000,'auth_cleanup_pass':True}
            rc=cw.run_comparison_phases(e,cfg(),aggregate,lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);self.assertTrue(any(len(x)==4 and x[1]==8200 and x[0]=='formal' for x in e.calls))
    def test_full_phases_keep_paired_policy(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td));cw.run_comparison_phases(e,cfg(),lambda r:{'benchmark_pass':True,'auth_cleanup_pass':True},lambda r:{'benchmark_pass':True})
            self.assertEqual(common.read_json(e.root_run/'formal/run-config.json')['intent_output_token_floor'],1024)
    def test_no_html_and_no_historical_completion(self):
        with tempfile.TemporaryDirectory() as td:
            r=minimal_report_run(Path(td));common.atomic_json(r/'run-config.json',cfg());out=cr.build_report(r)
            self.assertFalse(out['validation']['full_comparison_ready']);self.assertFalse((r/cr.HTML_FILE).exists())
            self.assertEqual([x['intent_output_token_floor'] for x in out['input_treatments']],[1024,1024])
    def test_gateway_launch_receives_explicit_policy(self):self.assertIn("'intent_output_token_floor':cfg.get('intent_output_token_floor')",(ROOT/'bench.py').read_text())
    def test_no_timeout_overrides_added(self):
        s=(ROOT/'generation_budget.py').read_text();self.assertNotIn('OPENAI_TIMEOUT',s);self.assertNotIn('request_timeout=',s)


class Loopback(unittest.TestCase):
    def trial(self,reply=None,mode='legacy_schema_fields',parallel=1):
        seen=[];lock=threading.Lock();fixed=reply
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with lock:seen.append(data)
                text=fixed if fixed is not None else content()
                raw=json.dumps({'choices':[{'message':{'content':text},'finish_reason':'stop'}],
                                'usage':{'prompt_tokens':100,'completion_tokens':400}},ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as td:
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);bridge=gw.Server(('127.0.0.1',0),gw.Handler)
            bridge.schema_mode=mode;bridge.intent_output_token_floor=1024;bridge.upstream=f'http://127.0.0.1:{model.server_port}'
            bridge.provider_port=8000 if mode=='legacy_schema_fields' else 8200;bridge.proxy_port=model.server_port
            bridge.local_key='synthetic';bridge.audit_log=str(Path(td)/'audit.jsonl');bridge.request_timeout=5
            threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.01},daemon=True) for s in (model,bridge)]
            for t in threads:t.start()
            try:
                def send(i):
                    return common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/responses',native.payload(),10,{'X-Request-ID':'shadow-'+f'{i:032x}'})
                with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:responses=list(pool.map(send,range(1,parallel+1)))
                for i in range(1,parallel+1):wait_for_audit_rows(bridge.audit_log,request_id='shadow-'+f'{i:032x}')
                rows=sm.json_events(Path(bridge.audit_log))
            finally:
                for s in (bridge,model):s.shutdown();s.server_close()
                for t in threads:t.join(2)
        self.assertEqual(len(seen),parallel);self.assertTrue(all(x['max_tokens']==1024 for x in seen));self.assertEqual(len(rows),parallel)
        for code,b,_ in responses:self.assertEqual(code,200);self.assertEqual(b['output'][0]['content'][0]['text'],fixed if fixed is not None else content())
        return rows
    def test_legacy_live_http_limit_and_original_reply(self):
        e=self.trial()[0];self.assertEqual(e['schema_validation'],'pass');self.assertEqual(e['requested_max_tokens'],400);self.assertEqual(e['effective_max_tokens'],1024)
    def test_native_live_http_same_limit(self):self.assertEqual(self.trial(mode='vllm_native_raw_query')[0]['native_bound_schema_validation'],'pass')
    def test_invalid_unicode_not_repaired(self):
        e=self.trial(r'{"x":"\uZZZZ"}')[0];self.assertEqual(e['schema_validation'],'fail');self.assertEqual(e['output_contract_diagnostics']['raw']['parse_error']['code'],'invalid_unicode_escape')
    def test_fence_still_fails(self):self.assertEqual(self.trial('```json\n'+content()+'\n```')[0]['schema_validation'],'fail')
    def test_enum_still_fails(self):self.assertEqual(self.trial(content(texture='SYNTHETIC_INVALID'))[0]['schema_validation'],'fail')
    def test_echo_still_fails(self):self.assertEqual(self.trial(content(query='wrong'))[0]['legacy_bound_schema_validation'],'fail')
    def test_native_8_concurrent_have_unique_ids_no_added_calls(self):
        rows=self.trial(mode='vllm_native_raw_query',parallel=8);self.assertEqual(len({x['gateway_call_id'] for x in rows}),8)
        self.assertTrue(all(gb.call_budget_verified(x,1024) for x in rows))
    def test_legacy_8_concurrent_have_unique_ids_no_added_calls(self):
        rows=self.trial(parallel=8);self.assertEqual(len({x['gateway_call_id'] for x in rows}),8)
        self.assertTrue(all(gb.call_budget_verified(x,1024) for x in rows))


class GuardedProxyBudget(unittest.TestCase):
    def trial(self,clamp=False,mode='legacy_schema_fields'):
        from test_endpoint_correlation import RealGuardedASGI
        original=gw.convert
        original_write=Path.write_text
        def adapt(payload,path,**kw):
            return original(native.payload(),'/v1/responses',schema_mode=mode,intent_output_token_floor=1024)
        def write(path,data,*args,**kw):
            if path.name=='asgi_fixture.py' and clamp:
                data=data.replace("body['messages'][-1]['content']='synthetic altered prompt'","body['max_tokens']=400")
            return original_write(path,data,*args,**kw)
        with patch.object(gw,'convert',side_effect=adapt),patch.object(Path,'write_text',write):
            return RealGuardedASGI().trial(alter=clamp)
    def test_actual_guarded_proxy_preserves_1024(self):
        result,models,_=self.trial();self.assertTrue(result['pass'],result)
        self.assertTrue(all(m['effective_max_tokens']==1024 for m in models))
        self.assertTrue(all(m['schema_validation']=='fail' for m in models))
    def test_actual_proxy_clamp_to_400_is_rejected(self):
        result,_,_=self.trial(clamp=True);self.assertFalse(result['pass']);self.assertIn('output_budget_wire_mismatch',result['failure_reasons'])
    def test_actual_native_proxy_preserves_same_budget(self):
        result,models,_=self.trial(mode='vllm_native_raw_query');self.assertTrue(result['pass'],result)
        self.assertTrue(all(m['generation_budget']['effective_max_tokens']==1024 for m in models))

if __name__=='__main__':unittest.main(verbosity=2)
