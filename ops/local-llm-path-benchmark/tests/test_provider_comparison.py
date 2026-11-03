"""Local synthetic and loopback tests only. No BLINK/TAIDE/GPU connection."""
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import common, bench, compat_gateway as gw, native_schema as ns
import provider_strategy as ps
import comparison_workflow as cw
import comparison_report as cr
import smoke_checks as smoke
import profile_backend as pb
import test_auth_workflow as auth_fixture
from test_concurrent_requests import Sampler
import test_native_schema as native_fixture

SID='a'*64

def config():
    c=json.loads((ROOT/'config.json').read_text())
    c.update(providers=[8000,8200],schema_mode=ps.COMPARISON_MODE,auth_mode='member',vllm_mode='shared',
             vus=[1,4,8],repetitions=1,snapshot_id=SID,synthetic_test_fixture=True)
    return c


def envelope():return {'model':'taide-12b','messages':[{'role':'user','content':'synthetic unique input'}]}

def endpoint_rows(port=8000):
    fp=ps.outgoing_metadata(json.dumps(envelope()))
    return ([{'provider_port':port,'endpoint_request_fingerprint':fp,'request_id':'synthetic'}],
            [{'role':'proxy','model_http':True,'host':'127.0.0.1','port':port,'path':'/v1/chat/completions','endpoint_wire':fp}])


class Strategies(unittest.TestCase):
    def test_pair_accepted(self):bench.validate_config(config())
    def test_legacy_wire_is_prompt_only(self):self.assertEqual(ps.strategy(config(),8000)['schema_mode'],'prompt_only')
    def test_legacy_label_not_native(self):self.assertEqual(ps.strategy(config(),8000)['contract_mode'],'legacy_prompt_contract')
    def test_vllm_native_wire(self):self.assertEqual(ps.strategy(config(),8200)['schema_mode'],'vllm_native_raw_query')
    def test_never_native_legacy(self):
        c=config();c['schema_mode']='vllm_native_raw_query'
        with self.assertRaises(common.Refuse):ps.strategy(c,8000)
    def test_native_global_pair_still_refused(self):
        c=config();c['schema_mode']='vllm_native_raw_query'
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_vu16_refused_in_first_comparison(self):
        c=config();c['vus'].append(16)
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_guest_refused(self):
        c=config();c['auth_mode']='guest'
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_owned_mode_refused(self):
        c=config();c['vllm_mode']='owned';c['providers']=[8000,8220]
        with self.assertRaises(common.Refuse):bench.validate_config(c)
    def test_old_prompt_only_config_retained(self):bench.validate_config(json.loads((ROOT/'config.json').read_text()))
    def test_strategy_does_not_mutate_config(self):
        c=config();old=copy.deepcopy(c);ps.strategy(c,8000);ps.strategy(c,8200);self.assertEqual(c,old)
    def test_alias_does_not_change_original_prompt(self):
        payload={'model':'anything','input':'synthetic message'}
        a,meta=gw.convert(payload,'/v1/responses',schema_mode=ps.strategy(config(),8000)['schema_mode'])
        b,_=gw.convert(payload,'/v1/responses',schema_mode='prompt_only')
        self.assertEqual(a,b);self.assertNotIn('response_format',a)
        self.assertEqual(meta['endpoint_request_fingerprint']['status'],'observed')


class EndpointEvidence(unittest.TestCase):
    def test_real_destination_and_hash_required(self):self.assertTrue(ps.endpoint_proof(*endpoint_rows(),8000)['pass'])
    def test_configuration_port_is_not_evidence(self):
        m,d=endpoint_rows();self.assertFalse(ps.endpoint_proof(m,[],8000)['pass'])
    def test_wrong_provider_label_fails(self):
        m,d=endpoint_rows();m[0]['provider_port']=8200;self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_wrong_destination_fails(self):
        m,d=endpoint_rows();d[0]['port']=8200;self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_wrong_host_fails(self):
        m,d=endpoint_rows();d[0]['host']='not-loopback.invalid';self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_messages_changed_fails(self):
        m,d=endpoint_rows();d[0]['endpoint_wire']=ps.outgoing_metadata(json.dumps({**envelope(),'messages':[{'role':'user','content':'changed'}]}))
        self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_same_requests_need_multiplicity(self):
        m,d=endpoint_rows();self.assertFalse(ps.endpoint_proof(m*2,d,8000)['pass'])
        self.assertTrue(ps.endpoint_proof(m*2,d*2,8000)['pass'])
    def test_extra_attempts_not_hidden_or_named_retry(self):
        m,d=endpoint_rows();r=ps.endpoint_proof(m,d*2,8000)
        self.assertTrue(r['pass']);self.assertEqual(r['extra_matching_attempts'],1)
    def test_no_requests_not_vacuous_pass(self):self.assertFalse(ps.endpoint_proof([],[],8000)['pass'])
    def test_unknown_wire_fails(self):
        m,d=endpoint_rows();del d[0]['endpoint_wire'];self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_nonmodel_dependency_not_used(self):
        m,d=endpoint_rows();d[0]['model_http']=False;self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])
    def test_hash_contains_no_prompt_or_personal_text(self):
        raw=json.dumps(envelope());out=json.dumps(ps.outgoing_metadata(raw));self.assertNotIn('synthetic unique input',out)
    def test_malformed_payload_unknown(self):self.assertEqual(ps.outgoing_metadata('bad json')['status'],'unavailable')
    def test_selected_endpoint_added_to_full_path_gate(self):
        row,ev=native_fixture.NativeFunctionalGate().fixture();ev['treatment']=ps.strategy(config(),8200)
        result=smoke.evaluate_request(row,'skin_consult',ev,'合成回答')
        self.assertFalse(result['full_path_pass']);self.assertIn('selected_model_endpoint_observed',result['failure_reasons'])
    def test_native_fingerprint_is_not_general_endpoint_proof(self):
        m,d=endpoint_rows();d[0]['native_wire']=d[0].pop('endpoint_wire');self.assertFalse(ps.endpoint_proof(m,d,8000)['pass'])


class FakeExperiment:
    def __init__(self, root, fail=None):
        self.root_run=root;self.run=root;self.snapshot_id=SID;self.calls=[];self.cfg=None;self.fail=fail;self.releases=0
    def set_phase(self,name,cfg):
        self.phase=name;self.cfg=copy.deepcopy(cfg);self.run=self.root_run/name;self.run.mkdir(exist_ok=True)
        common.atomic_json(self.run/'run-config.json',{**cfg,'snapshot_id':SID})
    def candidate_test(self):self.calls.append(('candidate',self.phase))
    def scenario(self,p,sc):
        self.calls.append((self.phase,p,sc['name'],self.snapshot_id))
        if self.fail and self.fail(self,p,sc):raise ps.ProviderGateFailure('PROVIDER_UNAVAILABLE')
        return {'status':'HTTP_BATCHES_PASS_PENDING_PATH_REVIEW'}
    def release_cell(self):self.releases+=1


class Workflow(unittest.TestCase):
    def run_case(self, bad=8000, fail=None, auth_ok=True, blocked=False, unexpected=False):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.e=FakeExperiment(Path(self.t.name),fail)
        def aggregate(run):
            p=self.e.cfg['providers'][0]
            if unexpected and self.e.phase=='smoke':raise RuntimeError('synthetic unexpected failure')
            return {'benchmark_pass':p!=bad,'auth_cleanup_pass':True,'blocked_attempts':[{}] if blocked else []}
        with patch.object(cw.time,'sleep') as sleeper:
            code=cw.run_comparison_phases(self.e,config(),aggregate,lambda r:{'benchmark_pass':auth_ok})
        return code,common.read_json(self.e.root_run/'comparison-gates.json'),sleeper
    def test_legacy_failure_does_not_block_vllm(self):
        code,g,_=self.run_case();self.assertEqual(code,2)
        cases=[x for x in self.e.calls if len(x)==4]
        self.assertEqual(len([x for x in cases if x[:2]==('smoke',8000)]),3)
        self.assertFalse(any(x[:2]==('formal',8000) for x in cases))
        self.assertEqual(len([x for x in cases if x[:2]==('formal',8200)]),3)
        self.assertEqual(g['providers'][0]['status'],'SMOKE_GATE_FAILED')
        self.assertEqual(g['providers'][1]['status'],'FORMAL_PASS')
    def test_same_snapshot_all_cases(self):
        self.run_case(bad=None);self.assertEqual({x[3] for x in self.e.calls if len(x)==4},{SID})
    def test_cooldown_between_providers(self):
        _,_,sleeper=self.run_case();sleeper.assert_called_once_with(65)
    def test_all_pass_return_zero(self):
        code,g,_=self.run_case(bad=None);self.assertEqual(code,0);self.assertEqual(g['status'],'COMPARISON_FUNCTIONAL_PASS')
    def test_shared_auth_failure_stops_all(self):
        code,g,_=self.run_case(auth_ok=False);self.assertEqual(code,2)
        self.assertFalse(any(x[0]=='smoke' for x in self.e.calls))
    def test_policy_block_is_global(self):
        code,g,_=self.run_case(blocked=True);self.assertEqual(code,2)
        self.assertFalse(any(len(x)==4 and x[1]==8200 for x in self.e.calls))
    def test_typed_dependency_failure_cleans_cell_and_continues(self):
        code,g,_=self.run_case(fail=lambda e,p,s:e.phase=='smoke' and p==8000)
        self.assertEqual(self.e.releases,1);self.assertEqual(g['providers'][1]['status'],'FORMAL_PASS')
    def test_unknown_exception_not_reclassified_as_provider_failure(self):
        with self.assertRaises(RuntimeError):self.run_case(unexpected=True)
        self.assertFalse(any(len(x)==4 and x[1]==8200 for x in self.e.calls))
    def test_combined_phase_plan_restored(self):
        self.run_case();self.assertEqual(common.read_json(self.e.root_run/'formal/run-config.json')['providers'],[8000,8200])
    def test_runtime_drift_stops_before_provider(self):
        with tempfile.TemporaryDirectory() as td:
            e=FakeExperiment(Path(td));e.capsule=Path(td)/'snapshot';e.capsule.mkdir()
            common.atomic_json(e.capsule/'runtime-metadata.json',{})
            with patch.object(cw.runtime,'runtime_changed',return_value={'status':'DRIFT'}):
                rc=cw.run_comparison_phases(e,config(),lambda r:{'benchmark_pass':True},lambda r:{'benchmark_pass':True})
            self.assertEqual(rc,3);self.assertFalse(any(x[0]=='smoke' for x in e.calls))
    def test_cleanup_failure_is_not_swallowed_to_continue(self):
        with tempfile.TemporaryDirectory() as td:
            e=FakeExperiment(Path(td),lambda e,p,s:e.phase=='smoke' and p==8000)
            with patch.object(e,'release_cell',side_effect=common.Refuse('synthetic owned cleanup failure')):
                with self.assertRaises(common.Refuse):
                    cw.run_comparison_phases(e,config(),lambda r:{'benchmark_pass':True},lambda r:{'benchmark_pass':True})
            self.assertFalse(any(len(x)==4 and x[1]==8200 for x in e.calls))
    def test_original_config_not_mutated(self):
        c=config();before=copy.deepcopy(c)
        with tempfile.TemporaryDirectory() as td,patch.object(cw.time,'sleep'):
            e=FakeExperiment(Path(td));cw.run_comparison_phases(e,c,lambda r:{'benchmark_pass':True},lambda r:{'benchmark_pass':True})
        self.assertEqual(c,before)
    def test_no_recapture_or_production_lifecycle_calls(self):
        import inspect
        text=inspect.getsource(cw)
        for value in ('frozen.capture(', 'start_owned(', 'terminate(', 'restore_candidate('):self.assertNotIn(value,text)


class BatchStop(unittest.TestCase):
    setUp=auth_fixture.IdentityFixture.setUp
    tearDown=auth_fixture.IdentityFixture.tearDown
    account=auth_fixture.IdentityFixture.account
    def trial(self, passed):
        cfg={'auth_mode':'member','request_timeout_seconds':3,'vus':[1,2], 'smoke_only':False,
             'repetitions':1,'batch_cooldown_seconds':0,'telemetry_interval_seconds':1,'snapshot_id':SID,'stop_on_path_failure':True}
        with tempfile.TemporaryDirectory() as td,patch.object(pb,'Sampler',Sampler),patch.object(pb,'save_smoke_assessment',
                return_value={'pass':True,'transport_guard_pass':True,'business_pass':True,'failure_reasons':[]}),patch.object(pb,'assess_batch',
                return_value={'pass':passed,'requests':[{'full_path_pass':passed}]}):
            result=pb.run_scenario(self.base,{'name':'product_search','message':'x','flow':'product_search'},cfg,Path(td),lambda:None,
                accounts=[self.account(1),self.account(2)])
            return result
    def test_http200_path_failure_stops_before_next_vu(self):
        r=self.trial(False);self.assertEqual(r['status'],'PATH_GATE_FAILED_STOPPED');self.assertEqual(len(r['batches']),1)
        self.assertEqual(self.server.valid_chat_count,3);self.assertEqual(len(self.server.sessions),0)
    def test_path_pass_continues_with_timestamps(self):
        r=self.trial(True);self.assertEqual(len(r['batches']),2);self.assertTrue(r['batches'][0]['started_at']);self.assertTrue(r['batches'][0]['finished_at'])


class WireLoopback(unittest.TestCase):
    def test_real_guarded_proxy_emits_general_endpoint_proof(self):
        _,events,deps=native_fixture.GuardedProxyHTTP().trial()
        port=events[0]['provider_port'];proof=ps.endpoint_proof(events,deps,port)
        self.assertTrue(proof['pass'],proof)
    def test_real_legacy_without_native_format_endpoint_proof(self):
        original=gw.convert
        with patch.object(gw,"convert",side_effect=lambda payload,path,**kw:original(payload,path,schema_mode="prompt_only")):
            _,events,deps=native_fixture.GuardedProxyHTTP().trial()
        self.assertEqual(events[0]["schema_mode"],"prompt_only")
        self.assertTrue(ps.endpoint_proof(events,deps,events[0]["provider_port"])["pass"])
        self.assertFalse(events[0].get("native_schema_requested",False))
        self.assertTrue(any(d.get("native_wire",{}).get("status")=="not_native_json_schema" for d in deps))
    def test_real_proxy_message_change_fails_endpoint_proof(self):
        _,events,deps=native_fixture.GuardedProxyHTTP().trial(alter_messages=True)
        self.assertFalse(ps.endpoint_proof(events,deps,events[0]['provider_port'])['pass'])


def minimal_report_run(root):
    run=root/'runs'/'synthetic';run.mkdir(parents=True)
    common.atomic_json(run/'run-config.json',config())
    common.atomic_json(run/'snapshot-manifest.json',{'snapshot_id':SID})
    common.atomic_json(run/'comparison-gates.json',{'snapshot_id':SID,'status':'COMPARISON_COMPLETED_WITH_PROVIDER_FAILURES',
        'shared_auth':{'pass':True},'providers':[{'provider_port':8000,'status':'SMOKE_GATE_FAILED'},
                                             {'provider_port':8200,'status':'FORMAL_GATE_FAILED'}]})
    common.atomic_json(run/'owned-cleanup-proof.json',{'pass':True,'snapshot_id':SID})
    common.atomic_json(run/'exit-status.json',{'exit_code':2,'private_state_removed':True})
    return run


def seed_case(run,p=8000,sc='skin_consult'):
    out=run/'formal'/f'provider-{p}'/sc;out.mkdir(parents=True)
    common.atomic_json(out/'treatment.json',{**ps.strategy(config(),p),'snapshot_id':SID,'phase':'formal'})
    bd=out/'repetition-1/vu-1';bd.mkdir(parents=True)
    rid='shadow-'+('b' if p==8000 else 'c')*32
    row={'request_id':rid,'status':200,'reply_nonempty':True,'correlated':True,'stage':'dispatch','route':'skin_consult',
         'body_ms':1000,'vu':1,'vu_index':1,'repetition':1,'scenario':sc,'snapshot_id':SID,
         'account_alias':'A01','auth_mode':'member','session_verified':True,'member_cookie_attached':True,'guest_cookie_attached':False,
         'identity_fingerprint':'synthetic-member-hash'}
    common.atomic_json(bd/'client-results.json',[row]);(bd/'reply-vu-1.txt').write_text('合成回覆；SYNTHETIC_PRIVATE_SENTINEL 不可匯出')
    common.atomic_json(bd/'summary.json',{'completed':1,'planned':1,'wall_seconds':2,'snapshot_id':SID,
        'telemetry':{'stats':{},'status':'synthetic'},'deployment_window':{'performance_eligible':True,'status':'NO_OBSERVED_DEPLOYMENT_DRIFT'}})
    m,d=endpoint_rows(p);m[0].update(request_id=rid,status=200,finish_reason='stop',schema_mode='none',duration_ms=100,
                                  usage={'prompt_tokens':4,'completion_tokens':2})
    cid='call-'+('1' if p==8000 else '2')*32
    iid='ingress-'+('3' if p==8000 else '4')*32
    m[0]['gateway_call_id']=cid
    d[0].update(gateway_call_id=cid,proxy_ingress_id=iid,proxy_parent_request_id=rid,
                proxy_observer='proxy_asgi_context_v1',proxy_context='active_ingress')
    for kind in ('proxy_request_started','proxy_request_completed'):
        common.event(out/'trace/proxy-audit.jsonl',kind,gateway_call_id=cid,proxy_ingress_id=iid,
                     request_id=rid,inference_path=True,proxy_observer='proxy_asgi_context_v1')
    common.event(out/'trace/model-calls.jsonl','model_call',**m[0])
    common.event(out/'trace/proxy-audit.jsonl','dependency_http',**d[0])
    common.event(out/'trace/asgi-timing.jsonl','asgi_request',request_id=rid,path='/api/chat/message',status=200,total_ms=999)
    common.event(out/'trace/auth-resolution.jsonl','auth_resolution',request_id=rid,auth_mode='member',identity_fingerprint='synthetic-member-hash')
    log=run/'formal/service-logs'/f'provider-{p}'/sc/'backend.log'
    for pipeline,stage in [('knowledge','final_generation_taide'),('root','memory_begin_turn')]:
        common.event(log,'pipeline_stage_completed',request_id=rid,pipeline=pipeline,stage=stage,status='success',duration_ms=300)
    with log.open('a') as f:f.write('raw profile: SYNTHETIC_PRIVATE_SENTINEL\n')
    return out,bd


class Reports(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.run=minimal_report_run(self.root)
    def test_report_cli_supports_isolated_python(self):
        result=subprocess.run([sys.executable,'-I','-B',str(ROOT/'comparison_report.py'),
                               '--run-dir',str(self.run)],cwd=self.root,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.run/cr.FILES[0]).is_file())
    def test_both_providers_have_39_planned_even_no_formal(self):
        d=cr.build_report(self.run);self.assertEqual([p['planned'] for p in d['providers']],[39,39]);self.assertEqual(len(d['batches']),18)
    def test_missing_latency_and_goodput_are_null(self):
        d=cr.build_report(self.run);self.assertTrue(all(b['full_path_goodput'] is None for b in d['batches']))
        self.assertTrue(all(p['full_path_body_ms']['p95'] is None for p in d['providers']))
    def test_empty_run_never_functional_pass(self):self.assertFalse(cr.build_report(self.run)['validation']['functional_all_planned_pass'])
    def test_terminal_provider_failures_can_complete_comparison_evidence_without_functional_pass(self):
        # Both providers have terminal gate statuses; clean evidence completeness is distinct from all-pass.
        d=cr.build_report(self.run)
        self.assertFalse(d['validation']['functional_all_planned_pass'])
        self.assertFalse(d['validation']['full_comparison_ready'])  # no provider gate proof files yet
    def test_performance_ready_remains_stricter_than_outcome_ready(self):
        d=cr.build_report(self.run)
        self.assertFalse(d['validation']['full_performance_comparison_ready'])
    def test_known_goodput_formula(self):
        seed_case(self.run);d=cr.build_report(self.run);b=next(b for b in d['batches'] if b['completed'])
        self.assertEqual(b['full_path_goodput'],.5);self.assertEqual(b['full_path_body_ms']['p95'],1000)
    def test_failed_measured_zero_goodput_not_null(self):
        out,bd=seed_case(self.run);p=out/'trace/proxy-audit.jsonl';p.write_text('')
        d=cr.build_report(self.run);b=next(b for b in d['batches'] if b['completed'])
        self.assertEqual(b['full_path_goodput'],0);self.assertIsNone(b['full_path_body_ms']['p95'])
    def test_snapshot_mismatch_is_visible(self):
        out,bd=seed_case(self.run);x=common.read_json(out/'treatment.json');x['snapshot_id']='d'*64;common.atomic_json(out/'treatment.json',x)
        d=cr.build_report(self.run);self.assertFalse(d['validation']['same_snapshot_verified']);self.assertFalse(d['validation']['experiment_evidence_complete'])
    def test_claimed_formal_pass_without_requests_is_contradiction(self):
        x=common.read_json(self.run/'comparison-gates.json');x['providers'][1]['status']='FORMAL_PASS'
        common.atomic_json(self.run/'comparison-gates.json',x)
        d=cr.build_report(self.run)
        self.assertIn('FORMAL_PASS_COUNT_CONTRADICTION_8200',d['validation']['issues'])
        self.assertFalse(d['validation']['experiment_evidence_complete'])
    def test_client_index_cannot_escape_into_another_reply_path(self):
        out,bd=seed_case(self.run);x=common.read_json(bd/'client-results.json');x[0]['vu_index']='../../private'
        common.atomic_json(bd/'client-results.json',x)
        with self.assertRaises(common.Refuse):cr.build_report(self.run)
    def test_json_report_does_not_export_raw_reply_or_log(self):
        seed_case(self.run);cr.build_report(self.run)
        for name in cr.FILES:self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL',(self.run/name).read_text())
    def test_share_archive_exact_allowlist(self):
        seed_case(self.run);cr.build_report(self.run);out=cr.pack_comparison(self.run)
        with tarfile.open(out) as t:self.assertEqual({Path(m.name).name for m in t.getmembers()},set(cr.FILES))
    def test_pack_refuses_failed_cleanup(self):
        cr.build_report(self.run);common.atomic_json(self.run/'owned-cleanup-proof.json',{'pass':False})
        with self.assertRaises(common.Refuse):cr.pack_comparison(self.run)
    def test_report_rejects_symlink(self):
        (self.run/'linked').symlink_to(self.root/'private-nonexistent')
        with self.assertRaises(common.Refuse):cr.build_report(self.run)
    def test_retry_and_memory_not_invented(self):
        seed_case(self.run);r=cr.build_report(self.run)['requests'][0]
        self.assertIsNone(r['retry_count']);self.assertIsNone(r['memory_persistence_pass'])
    def test_http200_missing_endpoint_fails(self):
        out,bd=seed_case(self.run);(out/'trace/proxy-audit.jsonl').write_text('')
        r=cr.build_report(self.run)['requests'][0]
        self.assertTrue(r['http_200_nonempty']);self.assertFalse(r['full_path_pass'])
    def test_not_a_microbenchmark(self):self.assertIn('純推論引擎',cr.build_report(self.run)['caveats'][0])
    def test_script_embedded_json_escapes_html(self):
        value=cr.render({'test':'</script><script>alert(1)</script>'})
        self.assertNotIn('</script><script>alert',value);self.assertIn('\\u003c/script',value)
    def test_synthetic_label_explicit(self):self.assertEqual(cr.build_report(self.run)['validation']['source'],'LOCAL_SYNTHETIC')


if __name__=='__main__':unittest.main(verbosity=2)
