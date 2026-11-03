#!/usr/bin/env python3
"""Real loopback auth/chat fixture with deployment hooks; no BLINK network."""
import json,sys,tempfile,unittest,copy
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_auth_workflow as fixture
import profile_backend as pb
import owned_vllm as mv
import bench,common

class Sampler:
    def __init__(self,*a):pass
    def start(self):pass
    def stop(self):pass
    def summary(self):return {'status':'synthetic-not-GPU-data','stats':{}}

class Requests(unittest.TestCase):
    setUp=fixture.IdentityFixture.setUp
    tearDown=fixture.IdentityFixture.tearDown
    account=fixture.IdentityFixture.account
    def run_case(self,hooks):
        cfg={'auth_mode':'member','request_timeout_seconds':3,'vus':[1,2], 'smoke_only':False,'repetitions':1,'batch_cooldown_seconds':0,'telemetry_interval_seconds':1,'snapshot_id':'same-source'}
        with tempfile.TemporaryDirectory() as td, patch.object(pb,'Sampler',Sampler),patch.object(pb,'save_smoke_assessment',return_value={'pass':True,'transport_guard_pass':True,'business_pass':True,'failure_reasons':[]}):
            result=pb.run_scenario(self.base,{'name':'product_search','message':'x','flow':'product_search'},cfg,Path(td),lambda:None,accounts=[self.account(1),self.account(2)],batch_hooks=hooks)
            artifacts={p.relative_to(td).as_posix():json.loads(p.read_text()) for p in Path(td).rglob('*.json')}
            return result,artifacts
    def test_unavailable_batch_no_chat_retry_then_next_independent_batch(self):
        class Hooks:
            calls=0
            def before(self,d):self.calls+=1;return None if self.calls==1 else {'fixture':1}
            def after(self,t,d):return {'status':'DEPENDENCY_UNAVAILABLE' if t is None else 'NO_OBSERVED_DEPLOYMENT_DRIFT','performance_eligible':bool(t)}
        result,art=self.run_case(Hooks());a,b=result['batches']
        self.assertEqual(a['completed'],0);self.assertIsNone(a['successful_rps']);self.assertFalse(a['chat_requests_retried'])
        self.assertEqual(b['completed'],2)
        # Exactly guest-control + member smoke + two formal chats. No retry of skipped VU1.
        self.assertEqual(self.server.valid_chat_count,4);self.assertEqual(len(self.server.sessions),0)
    def test_model_epoch_change_is_not_removed_from_successful_batch(self):
        class Hooks:
            def before(self,d):return {'x':1}
            def after(self,t,d):return {'status':'DEPENDENCY_DRIFT','performance_eligible':False,'changed_ports':[8200]}
        result,art=self.run_case(Hooks())
        self.assertEqual(self.server.valid_chat_count,5)
        self.assertTrue(all(b['all_pass'] for b in result['batches']))
        self.assertTrue(all(not b['deployment_window']['performance_eligible'] for b in result['batches']))
    def test_frozen_snapshot_id_persisted_each_request(self):
        class Hooks:
            def before(self,d):return {'x':1}
            def after(self,t,d):return {'status':'NO_OBSERVED_DEPLOYMENT_DRIFT','performance_eligible':True}
        result,art=self.run_case(Hooks())
        rows=[row for key,val in art.items() if key.endswith('client-results.json') for row in val]
        self.assertEqual(len(rows),3);self.assertTrue(all(r['snapshot_id']=='same-source' for r in rows))
        self.assertEqual({r['account_alias'] for r in rows},{'A01','A02'})
    def test_unknown_observation_never_reported_as_no_drift(self):
        class Hooks:
            def before(self,d):return {'x':1}
            def after(self,t,d):return {'status':'OBSERVATION_PARTIAL','performance_eligible':False}
        result,_=self.run_case(Hooks())
        self.assertTrue(all(b['deployment_window']['status']=='OBSERVATION_PARTIAL' for b in result['batches']))
        self.assertEqual(len(self.server.sessions),0)

class Assets(unittest.TestCase):
    def make(self,td):
        p=Path(td)/'weights';p.mkdir();(p/'config.json').write_text('{}');(p/'model.safetensors').write_bytes(b'synthetic,not real weights')
        return {'argv':['python','vllm','serve',str(p)]},p
    def test_owned_config_content_change_detected(self):
        with tempfile.TemporaryDirectory() as td:
            rec,p=self.make(td);a=mv.model_files(rec);(p/'config.json').write_text('{"changed":true}')
            self.assertNotEqual(a,mv.model_files(rec))
    def test_owned_weights_are_stat_only_not_false_hash_claim(self):
        with tempfile.TemporaryDirectory() as td:
            rec,p=self.make(td);r=mv.model_files(rec)
            self.assertIn('sha256',r['files']['config.json']);self.assertNotIn('sha256',r['files']['model.safetensors'])
    def test_pack_does_not_include_private_capsule(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td)/'runs'/'one';run.mkdir(parents=True)
            common.atomic_json(run/'run-config.json',{});common.atomic_json(run/'preflight-status.json',{})
            (run/'snapshot').mkdir();(run/'snapshot'/'source.py').write_text('private')
            with self.assertRaises(common.Refuse):bench.pack(run)
    def test_manual_pack_refuses_pending_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td)/'runs'/'one';run.mkdir(parents=True)
            common.atomic_json(run/'run-config.json',{});common.atomic_json(run/'preflight-status.json',{})
            (run/'CLEANUP_FAILED.txt').write_text('failed')
            with self.assertRaises(common.Refuse):bench.pack(run)

if __name__=='__main__':unittest.main(verbosity=2)
