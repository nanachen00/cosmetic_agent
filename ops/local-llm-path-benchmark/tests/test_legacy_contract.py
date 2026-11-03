"""Synthetic/loopback regressions. No real BLINK accounts, TAIDE or GPU calls."""
from __future__ import annotations
import copy
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import common
import compat_gateway as gw
import legacy_contract as lc
import native_schema as ns
import provider_strategy as ps
import comparison_report as cr
import comparison_workflow as cw
import failure_diagnostics as fd
import smoke_checks as sm
import bench
import test_native_schema as native
from test_provider_comparison import config, minimal_report_run, seed_case, FakeExperiment
from audit_support import wait_for_audit_rows


def convert(payload=None, path='/v1/responses'):
    return gw.convert(native.payload() if payload is None else payload,path,schema_mode=lc.MODE)


def explicit_config():
    c=config();c['legacy_contract_mode']=lc.MODE;return c


def content(query='我想了解差別。',intent='knowledge_query',texture='不明'):
    return json.dumps({'intent_type':intent,'texture_preference':texture,'raw_query':query},ensure_ascii=False)


class InputContract(unittest.TestCase):
    def test_original_request_and_schema_unmodified(self):
        p=native.payload();before=copy.deepcopy(p);out,meta=convert(p)
        self.assertEqual(p,before);self.assertEqual(meta['schema'],native.schema())
    def test_no_native_response_format(self):
        out,meta=convert();self.assertNotIn('response_format',out);self.assertNotIn('native_schema_requested',meta)
    def test_exact_last_user_prefix_preserved(self):
        p=native.payload();out,_=convert(p)
        self.assertTrue(out['messages'][-1]['content'].startswith(p['input'][-1]['content']+'\n\n'+lc.MARKER))
    def test_task_instructions_survive_system_dropping_template(self):
        p=native.payload();out,_=convert(p)
        user_only='\n'.join(m['content'] for m in out['messages'] if m['role']=='user')
        self.assertIn(p['instructions'],user_only);self.assertIn('【完整 JSON Schema】',user_only)
        self.assertIn('knowledge_query',user_only);self.assertIn('禁止 Markdown',user_only)
    def test_original_system_role_not_removed(self):
        p=native.payload();out,_=convert(p);self.assertEqual(out['messages'][0],{'role':'system','content':p['instructions']})
    def test_history_order_and_values_unchanged(self):
        p=native.payload();history=[{'role':'user','content':'older query'},{'role':'assistant','content':'older answer'}]
        p['input'][:0]=copy.deepcopy(history);out,_=convert(p);self.assertEqual(out['messages'][1:-1],history)
    def test_does_not_choose_intent(self):
        _,meta=convert();self.assertEqual(meta['_legacy_bound_schema']['properties']['intent_type'],native.schema()['properties']['intent_type'])
        self.assertFalse(meta['classification_labels_pinned'])
    def test_bound_schema_changes_only_known_input_echo(self):
        _,meta=convert();s=copy.deepcopy(meta['_legacy_bound_schema'])
        self.assertEqual(s['properties']['raw_query'].pop('enum'),['我想了解差別。']);self.assertEqual(s,native.schema())
    def test_query_taken_from_last_turn_not_history(self):
        p=native.payload('new');p['input'][:0]=[{'role':'user','content':ns.QUERY_MARKER+'「old」'},{'role':'assistant','content':'old answer'}]
        _,m=convert(p);self.assertEqual(m['_legacy_bound_schema']['properties']['raw_query']['enum'],['new'])
    def test_unicode_and_whitespace_echo_unchanged(self):
        q='  字串「引號」"\\\n😀 e\u0301\t  ';_,m=convert(native.payload(q));self.assertEqual(m['_legacy_bound_schema']['properties']['raw_query']['enum'],[q])
    def test_profile_prefix_does_not_enter_echo(self):
        p=native.payload();p['input'][-1]['content']='合成背景資料\n\n'+p['input'][-1]['content']
        _,m=convert(p);self.assertEqual(m['_legacy_bound_schema']['properties']['raw_query']['enum'],['我想了解差別。'])
    def test_missing_query_marker_refused(self):
        p=native.payload();p['input']='question'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_ambiguous_query_marker_refused(self):
        with self.assertRaises(gw.Unsupported):convert(native.payload(ns.QUERY_MARKER+'「duplicate」'))
    def test_conflicting_original_echo_enum_not_overwritten(self):
        p=native.payload();p['text']['format']['schema']['properties']['raw_query']['enum']=['other']
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_nonrequired_echo_refused(self):
        p=native.payload();p['text']['format']['schema']['required'].remove('raw_query')
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_unknown_echo_schema_refused(self):
        p=native.payload();p['text']['format']['name']='different'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_generic_schema_not_treated_as_intent(self):
        p=native.payload();p['text']['format']={'type':'json_schema','name':'fixture','schema':{'type':'object','properties':{'choice':{'enum':['a','b']}},'required':['choice']}}
        out,m=convert(p);self.assertFalse(m['legacy_raw_query_bound']);self.assertEqual(m['_legacy_bound_schema'],p['text']['format']['schema'])
        self.assertIn('["a","b"]',out['messages'][-1]['content'])
    def test_incompatible_schema_refused(self):
        p=native.payload();p['text']['format']['schema']['type']='made_up_type'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_missing_final_user_not_invented(self):
        p=native.payload();p['input'].append({'role':'assistant','content':'prefill'})
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_tool_calls_stay_refused(self):
        p=native.payload();p['tools']=[{'type':'function'}]
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_streaming_stays_refused(self):
        p=native.payload();p['stream']=True
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_late_system_stays_refused(self):
        p=native.payload();p['input'].append({'role':'system','content':'late'})
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_oversized_contract_refused_not_truncated(self):
        p=native.payload();p['instructions']='x'*(lc.MAX_CONTRACT_BYTES+1)
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_no_double_adaptation(self):
        p=native.payload();p['instructions']+=lc.MARKER
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_generation_parameters_unchanged(self):
        p=native.payload();p.update(top_p=.7,seed=42,stop=['STOP'],presence_penalty=.3,frequency_penalty=.2)
        out,_=convert(p);old,_=gw.convert(p,'/v1/responses')
        self.assertEqual({k:v for k,v in out.items() if k!='messages'},{k:v for k,v in old.items() if k!='messages'})
    def test_default_temperature_unchanged(self):
        p=native.payload();del p['temperature'];out,_=convert(p);self.assertEqual(out['temperature'],.1)
    def test_free_text_wire_and_metadata_unchanged(self):
        p={'input':'free text','instructions':'task','max_output_tokens':100}
        self.assertEqual(convert(p),gw.convert(p,'/v1/responses',schema_mode='prompt_only'))
    def test_json_object_contract(self):
        p={'messages':[{'role':'user','content':'object please'}],'response_format':{'type':'json_object'}}
        out,m=convert(p,'/v1/chat/completions');self.assertEqual(m['schema_mode'],lc.MODE)
        self.assertIn('第一個非空白字元必須為 {',out['messages'][-1]['content']);self.assertNotIn('response_format',out)
    def test_chat_and_responses_have_equivalent_wire(self):
        p=native.payload();c={'messages':[{'role':'system','content':p['instructions']},*p['input']],
            'response_format':{'type':'json_schema','json_schema':p['text']['format']},'max_tokens':400,'temperature':.1}
        self.assertEqual(convert(c,'/v1/chat/completions')[0],convert(p)[0])
    def test_fingerprint_matches_actual_forwarded_bytes(self):
        out,m=convert();self.assertEqual(m['endpoint_request_fingerprint'],ps.outgoing_metadata(json.dumps(out)))
    def test_public_fingerprints_contain_no_member_text(self):
        _,m=convert(native.payload('SYNTHETIC_PRIVATE_ECHO'))
        self.assertNotIn('SYNTHETIC_PRIVATE_ECHO',json.dumps(m['legacy_contract_fingerprint']))
    def test_conditional_enum_not_flattened(self):
        s={'anyOf':[{'properties':{'choice':{'enum':['a']}}},{'properties':{'choice':{'enum':['b']}}}]}
        self.assertEqual(lc.enum_rules(s),[])
    def test_enum_choices_derived_from_input_not_scenario(self):
        p=native.payload();p['text']['format']['schema']['properties']['intent_type']['enum']=['red','green']
        out,m=convert(p);self.assertIn('["red","green"]',out['messages'][-1]['content'])
        self.assertNotIn('knowledge_query',out['messages'][-1]['content'])


class StrategyAndGates(unittest.TestCase):
    def test_new_comparison_config_valid(self):bench.validate_config(explicit_config())
    def test_old_config_not_relabelled(self):self.assertEqual(ps.strategy(config(),8000)['contract_mode'],'legacy_prompt_contract')
    def test_new_strategy_stamped(self):
        s=ps.strategy(explicit_config(),8000);self.assertEqual(s['schema_mode'],lc.MODE);self.assertEqual(s['input_contract_version'],lc.VERSION)
    def test_8200_strategy_unchanged(self):self.assertEqual(ps.strategy(explicit_config(),8200),ps.strategy(config(),8200))
    def test_reject_unknown_legacy_mode(self):
        c=config();c['legacy_contract_mode']='repair_outputs'
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_no_standalone_unlabelled_treatment(self):
        c=explicit_config();c['schema_mode']='prompt_only'
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_cannot_route_legacy_adapter_to_8200(self):
        with self.assertRaises(common.Refuse):ps.strategy({'schema_mode':lc.MODE},8200)
    def test_serve_refuses_8200_before_binding(self):
        with patch.object(gw,'Server') as server:
            with self.assertRaises(gw.Unsupported):gw.serve(12345,12346,8200,'x','x',1,lc.MODE)
            server.assert_not_called()
    def test_serve_still_refuses_native_on_8000(self):
        with patch.object(gw,'Server') as server:
            with self.assertRaises(gw.Unsupported):gw.serve(12345,12346,8000,'x','x',1,ns.MODE)
            server.assert_not_called()
    def test_legacy_missing_validation_fails(self):
        row,ev=native.NativeFunctionalGate().fixture();m=ev['model_calls'][0]
        m.update(schema_mode=lc.MODE,schema_validation=None)
        r=sm.evaluate_request(row,'skin_consult',ev,'合成回答')
        self.assertFalse(r['checks']['schema_verified_when_requested']);self.assertEqual(r['format_validation_unverified'],1)
    def test_echo_validation_failure_fails_path(self):
        row,ev=native.NativeFunctionalGate().fixture();ev['model_calls'][0].update(legacy_contract_requested=True,legacy_bound_schema_validation='fail')
        r=sm.evaluate_request(row,'skin_consult',ev,'合成回答');self.assertFalse(r['checks']['legacy_input_echo_and_schema_verified'])
    def test_legacy_failure_category_not_unknown(self):
        self.assertIn('MODEL_SCHEMA_CONTRACT_FAILURE',fd.classify(['legacy_input_echo_and_schema_verified']))
    def test_smoke_contract_failure_records_legacy_and_continues_8200(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td));c=explicit_config()
            rc=cw.run_comparison_phases(e,c,lambda r:{'benchmark_pass':e.cfg['providers'][0]!=8000,'auth_cleanup_pass':True},lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);calls=[x for x in e.calls if len(x)==4]
            self.assertEqual(len([x for x in calls if x[:2]==('smoke',8000)]),3)
            self.assertFalse(any(x[:2]==('formal',8000) for x in calls))
            self.assertEqual(len([x for x in calls if x[:2]==('formal',8200)]),3)
            g=common.read_json(e.root_run/'comparison-gates.json')
            self.assertEqual(g['providers'][0]['status'],'SMOKE_GATE_FAILED')
            self.assertEqual(g['providers'][1]['status'],'FORMAL_PASS')
            # FakeExperiment has no real endpoint/case evidence, so the synthetic final
            # label remains incomplete even though the other provider was executed.
            self.assertIn(g['status'],{'COMPARISON_COMPLETED_WITH_PROVIDER_FAILURES','COMPARISON_INCOMPLETE_EVIDENCE'})
    def test_formal_contract_failure_stops_legacy_load_only_and_continues_8200(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td))
            def aggregate(_):
                p=e.cfg['providers'][0]
                return {'benchmark_pass':not (p==8000 and e.phase=='formal'),'auth_cleanup_pass':True}
            rc=cw.run_comparison_phases(e,explicit_config(),aggregate,lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);self.assertTrue(any(len(x)==4 and x[1]==8200 and x[0]=='formal' for x in e.calls))
    def test_provider_transport_failure_is_terminal_for_legacy_only(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td),lambda e,p,s:e.phase=='smoke' and p==8000)
            rc=cw.run_comparison_phases(e,explicit_config(),lambda r:{'benchmark_pass':True},lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);self.assertTrue(any(len(x)==4 and x[1]==8200 for x in e.calls));self.assertEqual(e.releases,1)
    def test_pass_continues_full_pair_same_snapshot(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td));rc=cw.run_comparison_phases(e,explicit_config(),lambda r:{'benchmark_pass':True,'auth_cleanup_pass':True},lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,0);formal=[x for x in e.calls if len(x)==4 and x[0]=='formal']
            self.assertEqual(len(formal),6);self.assertEqual(len({x[3] for x in formal}),1)
            self.assertEqual(common.read_json(e.root_run/'formal/run-config.json')['legacy_contract_mode'],lc.MODE)


class RawReplyLoopback(unittest.TestCase):
    def trial(self,text,p=None,path='/v1/responses',status=200):
        seen=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw=json.dumps({'choices':[{'message':{'content':text},'finish_reason':'stop'}],
                    'usage':{'completion_tokens':30}},ensure_ascii=False).encode()
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as td:
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);bridge=gw.Server(('127.0.0.1',0),gw.Handler)
            bridge.schema_mode=lc.MODE;bridge.upstream=f'http://127.0.0.1:{model.server_port}'
            bridge.provider_port=8000;bridge.proxy_port=model.server_port;bridge.local_key='synthetic'
            bridge.audit_log=str(Path(td)/'audit.jsonl');bridge.request_timeout=3
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (model,bridge)]
            for t in threads:t.start()
            try:
                code,result,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}'+path,native.payload() if p is None else p,5,{'X-Request-ID':'synthetic-legacy'})
                events=wait_for_audit_rows(bridge.audit_log,request_id='synthetic-legacy')
            finally:
                for s in (bridge,model):s.shutdown();s.server_close()
                for t in threads:t.join(2)
        self.assertEqual(len(seen),1,'No hidden generation retry permitted')
        actual=result['output'][0]['content'][0]['text'] if path.endswith('responses') and code==200 else result['choices'][0]['message']['content']
        self.assertEqual(actual,text,'Raw output must not be stripped/repaired/replaced')
        for key in ('schema','_native_schema','_legacy_bound_schema'):self.assertNotIn(key,events[0])
        return code,seen[0],events[0]
    def test_valid_json_passes_unchanged(self):
        c,out,e=self.trial(content());self.assertEqual(c,200);self.assertEqual(e['schema_validation'],'pass');self.assertEqual(e['legacy_bound_schema_validation'],'pass')
        self.assertNotIn('response_format',out);self.assertIn(lc.MARKER,out['messages'][-1]['content'])
    def test_fenced_valid_json_still_fails_raw_contract(self):
        _,_,e=self.trial('```json\n'+content()+'\n```');self.assertEqual(e['schema_validation'],'fail')
        self.assertEqual(e['output_contract_diagnostics']['fence_payload_diagnostic']['schema_validation'],'pass')
    def test_enum_mismatch_still_fails(self):
        _,_,e=self.trial(content(intent='invented_class'));self.assertEqual(e['schema_validation'],'fail');self.assertEqual(e['validation_keyword'],'enum')
    def test_wrong_echo_still_fails(self):
        _,_,e=self.trial(content(query='not the current question'));self.assertEqual(e['schema_validation'],'fail');self.assertEqual(e['legacy_bound_schema_validation'],'fail')
    def test_malformed_json_not_repaired(self):
        _,_,e=self.trial('{"intent_type":"knowledge_query",}');self.assertEqual(e['schema_validation'],'fail')
    def test_valid_alternative_enum_not_changed(self):
        _,_,e=self.trial(content(intent='unclear'));self.assertEqual(e['schema_validation'],'pass')
    def test_member_text_absent_from_public_audit(self):
        secret='SYNTHETIC_PRIVATE_QUERY';_,_,e=self.trial(content(query=secret),native.payload(secret));self.assertNotIn(secret,json.dumps(e,ensure_ascii=False))
    def test_chat_api_raw_output_unchanged(self):
        p={'messages':[{'role':'user','content':'give an object'}],'response_format':{'type':'json_object'}}
        _,_,e=self.trial('{"a":1}',p,'/v1/chat/completions');self.assertEqual(e['schema_validation'],'pass')
    def test_json_object_array_fails(self):
        p={'messages':[{'role':'user','content':'give an object'}],'response_format':{'type':'json_object'}}
        _,_,e=self.trial('[1]',p,'/v1/chat/completions');self.assertEqual(e['schema_validation'],'fail')
    def test_diagnostic_path_exported_without_value(self):
        _,_,e=self.trial(content(texture='SYNTHETIC_INVALID_ENUM_SECRET'))
        d=cr.safe_contract_diagnostic(e['output_contract_diagnostics']);self.assertEqual(d['raw']['instance_path'],['texture_preference'])
        self.assertEqual(d['raw']['schema_path'],['properties','texture_preference','enum'])
        self.assertNotIn('SYNTHETIC_INVALID_ENUM_SECRET',json.dumps(d))


class DataOnly(unittest.TestCase):
    def test_default_does_not_call_html_renderer(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cr,'render',side_effect=AssertionError('HTML should not run')):
            run=minimal_report_run(Path(td));d=cr.build_report(run)
            self.assertFalse((run/cr.HTML_FILE).exists());self.assertFalse(d['validation']['interactive_html_generated'])
            self.assertEqual(d['output_mode'],'data_only')
    def test_incomplete_html_request_refused(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td))
            with self.assertRaises(common.Refuse):cr.build_report(run,include_html=True)
            self.assertFalse((run/cr.HTML_FILE).exists())
    def test_archive_has_five_data_files_no_html(self):
        import tarfile
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));cr.build_report(run)
            (run/cr.HTML_FILE).write_text('old artifact not included')
            out=cr.pack_comparison(run)
            with tarfile.open(out) as t:names=[Path(m.name).name for m in t.getmembers()]
            self.assertEqual(set(names),set(cr.FILES));self.assertEqual(len(names),5);self.assertFalse(any(n.endswith('.html') for n in names))
    def test_both_formal_not_complete_has_no_ready_label(self):
        with tempfile.TemporaryDirectory() as td:
            d=cr.build_report(minimal_report_run(Path(td)));self.assertFalse(d['validation']['full_comparison_ready'])
    def test_diagnostic_paths_are_bounded(self):
        v={'raw':{'instance_path':['safe','../outside','email@example.invalid',-2,1000001,{},'texture_preference']}}
        got=cr.safe_contract_diagnostic(v)['raw']['instance_path']
        self.assertEqual(got,['safe','<field>','<field>','<field>','<field>','<field>','texture_preference'])
    def test_existing_measurements_not_relabelled(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));d=cr.build_report(run)
            self.assertEqual(d['providers'][0]['contract_mode'],'legacy_prompt_contract')
    def test_new_run_treatment_public(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));common.atomic_json(run/'run-config.json',explicit_config())
            d=cr.build_report(run);self.assertEqual(d['providers'][0]['contract_mode'],lc.MODE)
            self.assertEqual(d['input_treatments'][0]['input_contract_version'],lc.VERSION)
    def test_failed_run_never_promoted_to_ready(self):
        with tempfile.TemporaryDirectory() as td:
            run=minimal_report_run(Path(td));seed_case(run)
            d=cr.build_report(run);self.assertFalse(d['validation']['full_comparison_ready'])


if __name__=='__main__':unittest.main(verbosity=2)
