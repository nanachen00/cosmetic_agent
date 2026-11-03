"""Synthetic/loopback regressions for the field-contract treatment.

The malformed JSON cases reproduce failure classes, not undisclosed server reply
bytes. These tests verify harness behavior, not real TAIDE model compliance.
"""
from __future__ import annotations
import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import common, bench, compat_gateway as gw, native_schema as ns
import legacy_contract as old, legacy_schema_fields as fields
import provider_strategy as ps, smoke_checks as smoke, comparison_workflow as cw
import comparison_report as cr
import test_native_schema as native
from test_provider_comparison import config, FakeExperiment, minimal_report_run
from test_legacy_contract import content
from audit_support import wait_for_audit_rows


def cfg():
    c=config();c['legacy_contract_mode']=fields.MODE;return c


def convert(payload=None,path='/v1/responses'):
    return gw.convert(native.payload() if payload is None else payload,path,schema_mode=fields.MODE)


class InputContract(unittest.TestCase):
    def test_original_inputs_immutable(self):
        p=native.payload();before=copy.deepcopy(p);convert(p);self.assertEqual(p,before)
    def test_system_not_duplicated_into_user(self):
        p=native.payload();p['instructions']='UNIQUE_SYNTHETIC_TASK_SENTINEL'
        out,m=convert(p);self.assertEqual(sum(x['content'].count(p['instructions']) for x in out['messages']),1)
        self.assertEqual(m['legacy_contract_fingerprint']['system_instruction_copies'],0)
    def test_system_and_history_preserved(self):
        p=native.payload();p['input']=[{'role':'user','content':'old turn'},{'role':'assistant','content':'old answer'},*p['input']]
        out,_=convert(p);base,_=gw.convert(p,'/v1/responses',schema_mode=old.MODE)
        self.assertEqual(out['messages'][:-1],base['messages'][:-1])
        self.assertTrue(out['messages'][-1]['content'].startswith(p['input'][-1]['content']+'\n\n'))
    def test_contract_distinct_from_v1(self):
        out,m=convert();self.assertEqual(m['schema_mode'],fields.MODE);self.assertEqual(m['legacy_contract_version'],fields.VERSION)
        self.assertIn(fields.MARKER,out['messages'][-1]['content']);self.assertNotIn(old.MARKER,out['messages'][-1]['content'])
    def test_only_input_echo_is_bound(self):
        _,m=convert();s=copy.deepcopy(m['_legacy_bound_schema'])
        self.assertEqual(s['properties']['raw_query'].pop('enum'),['我想了解差別。']);self.assertEqual(s,native.schema())
        self.assertFalse(m['classification_labels_pinned']);self.assertFalse(m['response_repaired'])
    def test_escaped_echo_is_exact_input_not_new_text(self):
        q='  "含引號" \\ 路徑\n😀 e\u0301\t  '
        out,m=convert(native.payload(q));self.assertEqual(m['_legacy_bound_schema']['properties']['raw_query']['enum'],[q])
        self.assertIn(old.canonical(q),out['messages'][-1]['content'])
    def test_v2_contract_requires_compact_direct_unicode(self):
        out,m=convert(native.payload('我想了解差別。'));text=out['messages'][-1]['content']
        self.assertEqual(fields.VERSION,'legacy_schema_fields_v2')
        self.assertEqual(m['legacy_serialization_profile'],fields.SERIALIZATION_PROFILE)
        self.assertIn('緊湊',text);self.assertIn('不得改寫成 \\uXXXX',text)
        self.assertIn('不可寫成「\\u6e2c\\u8a66」',text)
        self.assertIn('我想了解差別。',text)
        self.assertNotIn('\\u6211\\u60f3',text)
    def test_full_schema_remains_canonical_direct_unicode(self):
        p=native.payload();out,m=convert(p);contract=out['messages'][-1]['content']
        self.assertIn(old.canonical(m['_legacy_bound_schema']),contract)
        self.assertIn('"不明"',contract);self.assertNotIn('\\u4e0d\\u660e',contract)
    def test_no_token_or_sampling_parameter_changes(self):
        p=native.payload();p.update(top_p=.7,seed=42,stop=['STOP'],presence_penalty=.2,frequency_penalty=.3)
        out,_=convert(p);v1,_=gw.convert(p,'/v1/responses',schema_mode=old.MODE)
        self.assertEqual({k:v for k,v in out.items() if k!='messages'},{k:v for k,v in v1.items() if k!='messages'})
    def test_plain_text_has_identical_wire(self):
        p={'input':'plain','instructions':'unchanged','max_output_tokens':20}
        self.assertEqual(convert(p),gw.convert(p,'/v1/responses',schema_mode='prompt_only'))
    def test_actual_forwarded_fingerprint(self):
        out,m=convert();self.assertEqual(m['endpoint_request_fingerprint'],ps.outgoing_metadata(json.dumps(out)))
    def test_unknown_intent_schema_not_guessed(self):
        p=native.payload();p['text']['format']['name']='unknown'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_double_field_contract_rejected(self):
        p=native.payload();p['instructions']+=fields.MARKER
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_v1_contract_not_stacked(self):
        p=native.payload();p['instructions']+=old.MARKER
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_wrong_input_marker_not_repaired(self):
        p=native.payload();p['input']='missing marker'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_no_new_turn_invented(self):
        p=native.payload();p['input'][-1]['role']='assistant'
        with self.assertRaises(gw.Unsupported):convert(p)
    def test_json_object_does_not_become_native(self):
        out,m=convert({'messages':[{'role':'user','content':'object'}],'response_format':{'type':'json_object'}},'/v1/chat/completions')
        self.assertNotIn('response_format',out);self.assertEqual(m['legacy_contract_version'],fields.VERSION)
    def test_chat_and_responses_equivalent(self):
        p=native.payload();c={'messages':[{'role':'system','content':p['instructions']},*p['input']],
            'response_format':{'type':'json_schema','json_schema':p['text']['format']},'max_tokens':400,'temperature':.1}
        self.assertEqual(convert(p)[0],convert(c,'/v1/chat/completions')[0])
    def test_no_member_text_in_public_metadata(self):
        q='PRIVATE_QUERY_SENTINEL';_,m=convert(native.payload(q))
        self.assertNotIn(q,json.dumps(m['legacy_contract_fingerprint']))
    def test_v1_still_has_original_version_and_instruction_copy(self):
        p=native.payload();out,m=gw.convert(p,'/v1/responses',schema_mode=old.MODE)
        self.assertEqual(m['legacy_contract_version'],old.VERSION)
        self.assertIn(p['instructions'],out['messages'][-1]['content'])


class FieldGuide(unittest.TestCase):
    def test_enum_items_are_individual_json_literals(self):
        s={'type':'object','properties':{'texture_preference':{'type':'string','enum':['清爽','不明']}}}
        text='\n'.join(fields.field_rules(s));self.assertIn('1) "清爽"',text);self.assertIn('2) "不明"',text)
    def test_arbitrary_enum_not_test_scenario_labels(self):
        p=native.payload();p['text']['format']['schema']['properties']['intent_type']['enum']=['red','green']
        out,_=convert(p);text=out['messages'][-1]['content']
        self.assertIn('1) "red"',text);self.assertNotIn('knowledge_query',text)
    def test_null_string_and_null_literal_are_not_conflated(self):
        text='\n'.join(fields.field_rules({'enum':[None,'null',False,0]}))
        for token in ['1) null','2) "null"','3) false','4) 0']:self.assertIn(token,text)
    def test_local_ref_enum_in_guide(self):
        s={'$defs':{'choice':{'enum':['a','b']}},'properties':{'x':{'$ref':'#/$defs/choice'}}}
        self.assertIn('1) "a"','\n'.join(fields.field_rules(s)))
    def test_ref_sibling_constraints_not_overwritten(self):
        s={'$defs':{'choice':{'enum':['a','b']}},'properties':{'x':{'$ref':'#/$defs/choice','enum':['b']}}}
        t='\n'.join(fields.field_rules(s));self.assertIn('1) "b"',t);self.assertIn('1) "a"',t)
    def test_alternatives_not_flattened(self):
        s={'anyOf':[{'enum':['a']},{'enum':['b']}]};text='\n'.join(fields.field_rules(s))
        self.assertIn('anyOf',text);self.assertNotIn('1) "a"',text)
    def test_nested_array_field_guide(self):
        s={'properties':{'items':{'type':'array','items':{'type':'object','properties':{'kind':{'enum':['a','b']}}}}}}
        self.assertIn('$["items"][*]["kind"]','\n'.join(fields.field_rules(s)))
    def test_default_not_a_selected_answer(self):
        s={'properties':{'x':{'default':'SYNTHETIC_DEFAULT','type':'string'}}}
        t='\n'.join(fields.field_rules(s));self.assertIn('default',t);self.assertNotIn('SYNTHETIC_DEFAULT',t)
    def test_ref_cycles_bounded(self):
        s={'$defs':{'node':{'properties':{'child':{'$ref':'#/$defs/node'}}}},'$ref':'#/$defs/node'}
        self.assertIn('遞迴參照','\n'.join(fields.field_rules(s)))
    def test_root_recursive_reference_is_local_and_bounded(self):
        s={'properties':{'child':{'$ref':'#'}}}
        self.assertIn('遞迴參照','\n'.join(fields.field_rules(s)))
    def test_invalid_ref_refused(self):
        with self.assertRaises(common.Refuse):fields.field_rules({'$ref':'#/$defs/missing'})
    def test_no_external_reference_fetch(self):
        with self.assertRaises(common.Refuse):fields.field_rules({'$ref':'https://example.invalid/schema'})
    def test_oversize_not_silently_truncated(self):
        s={'properties':{str(i):{'type':'string'} for i in range(fields.MAX_FIELDS+1)}}
        with self.assertRaises(common.Refuse):fields.field_rules(s)
    def test_full_complex_schema_still_preserved(self):
        p=native.payload();s=p['text']['format']['schema']
        s['properties'].update({f'field_{i}':{'type':['string','null'],'enum':[None,'不明','可用'],
            'description':'合成約束，不是實際 production schema'} for i in range(20)})
        s['required']+=list(f'field_{i}' for i in range(20))
        out,m=convert(p);bound=copy.deepcopy(m['_legacy_bound_schema']);bound['properties']['raw_query'].pop('enum')
        self.assertEqual(bound,s);self.assertIn(old.canonical(m['_legacy_bound_schema']),out['messages'][-1]['content'])


class StrategyAndWorkflow(unittest.TestCase):
    def test_config_and_selected_version(self):
        bench.validate_config(cfg());s=ps.strategy(cfg(),8000)
        self.assertEqual(s['schema_mode'],fields.MODE);self.assertEqual(s['input_contract_version'],fields.VERSION)
    def test_8200_strategy_is_unchanged(self):self.assertEqual(ps.strategy(cfg(),8200),ps.strategy(config(),8200))
    def test_wrong_provider_refused_before_bind(self):
        with patch.object(gw,'Server') as server:
            with self.assertRaises(gw.Unsupported):gw.serve(12345,12346,8200,'x','x',1,fields.MODE)
            server.assert_not_called()
    def test_stale_v1_call_does_not_pass_new_treatment(self):
        row,ev=native.NativeFunctionalGate().fixture();m=ev['model_calls'][0]
        ev['treatment']={'schema_mode':fields.MODE};m.update(schema_mode=old.MODE,legacy_contract_requested=True,
             legacy_contract_version=old.VERSION,classification_labels_pinned=False,response_repaired=False)
        self.assertFalse(smoke.evaluate_request(row,'skin_consult',ev,'合成')['checks']['legacy_explicit_contract_applied'])
    def test_unknown_schema_validation_still_fails(self):
        row,ev=native.NativeFunctionalGate().fixture();ev['model_calls'][0].update(schema_mode=fields.MODE,schema_validation=None)
        self.assertFalse(smoke.evaluate_request(row,'skin_consult',ev,'合成')['checks']['schema_verified_when_requested'])
    def test_new_contract_and_echo_require_success(self):
        row,ev=native.NativeFunctionalGate().fixture();m=ev['model_calls'][0]
        ev['treatment']={'schema_mode':fields.MODE,'legacy_serialization_profile':fields.SERIALIZATION_PROFILE}
        m.update(schema_mode=fields.MODE,legacy_contract_requested=True,legacy_contract_version=fields.VERSION,
                 legacy_serialization_profile=fields.SERIALIZATION_PROFILE,
                 schema_validation='pass',legacy_bound_schema_validation='fail',classification_labels_pinned=False,response_repaired=False)
        r=smoke.evaluate_request(row,'skin_consult',ev,'合成');self.assertTrue(r['checks']['legacy_explicit_contract_applied'])
        self.assertFalse(r['checks']['legacy_input_echo_and_schema_verified'])
    def test_stale_v1_serialization_cannot_pass_v2_treatment(self):
        row,ev=native.NativeFunctionalGate().fixture();m=ev['model_calls'][0]
        ev['treatment']={'schema_mode':fields.MODE,'legacy_serialization_profile':fields.SERIALIZATION_PROFILE}
        m.update(schema_mode=fields.MODE,legacy_contract_requested=True,legacy_contract_version='legacy_schema_fields_v1',
                 legacy_serialization_profile=None,schema_validation='pass',legacy_bound_schema_validation='pass',
                 classification_labels_pinned=False,response_repaired=False)
        r=smoke.evaluate_request(row,'skin_consult',ev,'合成')
        self.assertFalse(r['checks']['legacy_explicit_contract_applied'])
    def test_legacy_smoke_failure_stops_legacy_formal_but_continues_8200(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td))
            def aggregate(_):
                return {'benchmark_pass':e.cfg['providers'][0]!=8000,'auth_cleanup_pass':True}
            rc=cw.run_comparison_phases(e,cfg(),aggregate,lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);calls=[x for x in e.calls if len(x)==4 and x[0] in {'smoke','formal'}]
            self.assertEqual(len([x for x in calls if x[:2]==('smoke',8000)]),3)
            self.assertFalse(any(x[:2]==('formal',8000) for x in calls))
            self.assertEqual(len([x for x in calls if x[:2]==('formal',8200)]),3)
    def test_both_pass_use_one_snapshot_no_replay(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td));rc=cw.run_comparison_phases(e,cfg(),lambda r:{'benchmark_pass':True,'auth_cleanup_pass':True},lambda r:{'benchmark_pass':True})
            calls=[x for x in e.calls if len(x)==4 and x[0] in {'smoke','formal'}];self.assertEqual(rc,0);self.assertEqual(len(calls),12)
            self.assertEqual(len({x[3] for x in calls}),1)
    def test_formal_failure_stops_legacy_load_but_continues_8200(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td))
            def aggregate(_):
                p=e.cfg['providers'][0]
                return {'benchmark_pass':not (p==8000 and e.phase=='formal'),'auth_cleanup_pass':True}
            rc=cw.run_comparison_phases(e,cfg(),aggregate,lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,2);self.assertTrue(any(len(x)==4 and x[1]==8200 and x[0]=='formal' for x in e.calls))


class Loopback(unittest.TestCase):
    def trial(self,text,p=None):
        seen=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw=json.dumps({'choices':[{'message':{'content':text},'finish_reason':'stop'}],
                                'usage':{'prompt_tokens':400,'completion_tokens':40}},ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as td:
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);bridge=gw.Server(('127.0.0.1',0),gw.Handler)
            bridge.schema_mode=fields.MODE;bridge.upstream=f'http://127.0.0.1:{model.server_port}';bridge.provider_port=8000
            bridge.proxy_port=model.server_port;bridge.local_key='synthetic';bridge.audit_log=str(Path(td)/'audit.jsonl');bridge.request_timeout=3
            threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.02},daemon=True) for s in (model,bridge)]
            for t in threads:t.start()
            try:
                code,result,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/responses',native.payload() if p is None else p,5,{'X-Request-ID':'synthetic-field'})
                rows=wait_for_audit_rows(bridge.audit_log,request_id='synthetic-field')
            finally:
                for s in (bridge,model):s.shutdown();s.server_close()
                for t in threads:t.join(2)
        self.assertEqual(code,200);self.assertEqual(len(seen),1);self.assertEqual(result['output'][0]['content'][0]['text'],text)
        self.assertEqual(rows[0]['legacy_contract_version'],fields.VERSION)
        self.assertEqual(rows[0]['legacy_serialization_profile'],fields.SERIALIZATION_PROFILE)
        return rows[0]
    def test_valid_reply_unchanged_pass(self):self.assertEqual(self.trial(content())['schema_validation'],'pass')
    def test_auto_root_texture_enum_failure_retained(self):
        e=self.trial(content(texture='SYNTHETIC_INVALID_TEXTURE'));self.assertEqual(e['schema_validation'],'fail')
        self.assertEqual(e['output_contract_diagnostics']['raw']['instance_path'],['texture_preference'])
    def test_product_malformed_json_retained_no_repair(self):
        text=content().replace(', "texture_preference"',' "texture_preference"')
        self.assertEqual(self.trial(text)['schema_validation'],'fail')
    def test_valid_fenced_payload_not_used_to_pass(self):self.assertEqual(self.trial('```json\n'+content()+'\n```')['schema_validation'],'fail')
    def test_wrong_raw_query_remains_fail(self):self.assertEqual(self.trial(content(query='wrong'))['legacy_bound_schema_validation'],'fail')
    def test_whitespace_enum_hint_does_not_trim_output(self):
        e=self.trial(content(texture=' 不明 '));self.assertEqual(e['schema_validation'],'fail')
        self.assertTrue(e['output_contract_diagnostics']['raw']['enum_mismatch']['matches_after_strip'])
    def test_private_reply_and_echo_absent_from_audit(self):
        e=self.trial(content(query='PRIVATE_SENTINEL'),native.payload('PRIVATE_SENTINEL'))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(e,ensure_ascii=False))
        for key in ('schema','_native_schema','_legacy_bound_schema'):self.assertNotIn(key,e)
    def test_alternative_class_not_forced_to_test_answer(self):self.assertEqual(self.trial(content(intent='unclear'))['schema_validation'],'pass')
    def test_concurrent_conversion_has_isolated_echo(self):
        queries=['synthetic-query-'+str(i) for i in range(32)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda q:convert(native.payload(q)),queries))
        for q,(out,m) in zip(queries,results):self.assertEqual(m['_legacy_bound_schema']['properties']['raw_query']['enum'],[q])
        self.assertEqual(len({m['endpoint_request_fingerprint']['request_sha256'] for _,m in results}),32)

    def test_real_concurrent_http_preserves_each_echo_and_single_attempt(self):
        seen=[];lock=threading.Lock()
        class EchoModel(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                messages=copy.deepcopy(body['messages'])
                messages[-1]['content']=messages[-1]['content'].split('\n\n'+fields.MARKER,1)[0]
                query=ns.current_query(messages)
                with lock:seen.append(query)
                raw=json.dumps({'choices':[{'message':{'content':content(query=query)},'finish_reason':'stop'}]},ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as td:
            model=ThreadingHTTPServer(('127.0.0.1',0),EchoModel);bridge=gw.Server(('127.0.0.1',0),gw.Handler)
            bridge.schema_mode=fields.MODE;bridge.upstream=f'http://127.0.0.1:{model.server_port}';bridge.provider_port=8000
            bridge.proxy_port=model.server_port;bridge.local_key='synthetic';bridge.audit_log=str(Path(td)/'audit.jsonl');bridge.request_timeout=5
            threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.02},daemon=True) for s in (model,bridge)]
            for t in threads:t.start()
            queries=['synthetic-http-query-'+str(i) for i in range(8)]
            def send(q):
                rid='synthetic-rid-'+q.rsplit('-',1)[1]
                code,body,_=common.http_json(f'http://127.0.0.1:{bridge.server_port}/v1/responses',native.payload(q),10,{'X-Request-ID':rid})
                all_rows=wait_for_audit_rows(bridge.audit_log,request_id=rid)
                rows=[row for row in all_rows if row.get('event')=='model_call' and row.get('request_id')==rid]
                return q,code,body,rows
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(send,queries))
            finally:
                for server in (bridge,model):server.shutdown();server.server_close()
                for thread in threads:thread.join(2)
            for q,code,body,rows in results:
                self.assertEqual(code,200);self.assertEqual(json.loads(body['output'][0]['content'][0]['text'])['raw_query'],q)
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['legacy_bound_schema_validation'],'pass')
            self.assertEqual(len(Path(bridge.audit_log).read_text().splitlines()),8)
            self.assertEqual(sorted(seen),sorted(queries))
            self.assertEqual(len({rows[0]['gateway_call_id'] for _,_,_,rows in results}),8)


class DiagnosticsAndDataOnly(unittest.TestCase):
    def test_parser_wording_variants_classified_without_text(self):
        for msg,expected in [("Expecting ',' delimiter or '}'",'expecting_comma'),("Expecting ':' delimiter after property name",'expecting_colon'),('Extra data after JSON','extra_data')]:
            self.assertEqual(gw._json_error_code(json.JSONDecodeError(msg,'PRIVATE_DOC',0)),expected)
    def test_syntax_eof_and_character_class(self):
        d=gw.output_contract_diagnostics('{"x":',{'type':'object'},'json_schema')
        self.assertTrue(d['raw']['parse_error']['at_end_of_output']);self.assertEqual(d['raw']['parse_error']['character_class'],'end_of_output')
    def test_enum_mismatch_export_has_no_literal(self):
        secret='PRIVATE_INVALID_VALUE';d=gw.output_contract_diagnostics(content(texture=secret),native.schema(),'json_schema')
        out=cr.safe_contract_diagnostic(d);self.assertNotIn(secret,json.dumps(out));self.assertEqual(out['raw']['enum_mismatch']['allowed_value_count'],2)
    def test_token_limit_diagnostic_preserved_not_truncation_proof(self):
        d=gw.output_contract_diagnostics('{}',{'type':'object'},'json_schema',10,{'completion_tokens':10})
        self.assertTrue(cr.safe_contract_diagnostic(d)['reported_completion_reached_requested_limit'])
    def test_unknown_enum_values_and_exception_text_dropped(self):
        d={'raw':{'enum_mismatch':{'allowed_value_count':2,'allowed_types':['string','PRIVATE'],'values':['PRIVATE'],'actual':'PRIVATE'},
           'parse_error':{'code':'expecting_comma','doc':'PRIVATE','character_class':'PRIVATE'}}}
        self.assertNotIn('PRIVATE',json.dumps(cr.safe_contract_diagnostic(d)))
    def test_malformed_diagnostic_metadata_is_not_executed_or_exported(self):
        d={'raw':{'enum_mismatch':{'allowed_types':[{},[],True,'string']},
                  'parse_error':{'character_class':{'private':'SECRET'}}}}
        out=cr.safe_contract_diagnostic(d);self.assertNotIn('SECRET',json.dumps(out))
        self.assertEqual(out['raw']['enum_mismatch']['allowed_types'],['string'])
    def test_new_run_no_html_and_no_false_complete_label(self):
        with tempfile.TemporaryDirectory() as td,patch.object(cr,'render',side_effect=AssertionError('HTML not requested')):
            run=minimal_report_run(Path(td));common.atomic_json(run/'run-config.json',cfg());d=cr.build_report(run)
            self.assertEqual(d['input_treatments'][0]['input_contract_version'],fields.VERSION)
            self.assertFalse(d['validation']['full_comparison_ready']);self.assertFalse((run/cr.HTML_FILE).exists())
            with tarfile.open(cr.pack_comparison(run)) as t:self.assertEqual({Path(x.name).name for x in t.getmembers()},set(cr.FILES))


if __name__=='__main__':unittest.main(verbosity=2)
