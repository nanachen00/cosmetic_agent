import contextlib,copy,datetime,hashlib,importlib.util,json,signal,unittest
from pathlib import Path
from unittest.mock import patch,Mock

spec=importlib.util.spec_from_file_location('legacy_retirement',Path(__file__).with_name('retire-legacy-mi300x.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class RetirementGuards(unittest.TestCase):
    def fixture(self,service='taide_gateway8000'):
        argv=['/usr/bin/python3',str(module.ROOT/'blink-ai/ops/runtime/taide_vllm_gateway.py'),'--port','8000']
        if service=='taide_engine8200':argv=['/usr/bin/python3','-m','vllm.entrypoints.openai.api_server','--model',str(module.ROOT/'blink-runtime/data/models/taide-12b'),'--served-model-name','taide-12b','--port','8200']
        proc=dict(pid=101,start_ticks='44',ppid=1,state='S',argv=argv,cwd=str(module.ROOT/'blink-ai'),executable='/usr/bin/python3.10',command_sha256=hashlib.sha256('\0'.join(argv).encode()).hexdigest())
        listener=dict(port=module.SERVICES[service],pids=[101],local_address='127.0.0.1:'+str(module.SERVICES[service]))
        row={key:value for key,value in proc.items() if key!='argv'};row.update(service=service,port=module.SERVICES[service],status='running',command=argv,listeners=[listener],managed_identity=dict(status='not_found',records=[]))
        rows=[row if name==service else dict(service=name,port=port,status='not_listening') for name,port in module.SERVICES.items()]
        identity=dict(hostname='school',container_id='container',pod_uid='pod')
        plan=dict(schema_version=1,created_at_utc=module.utc().isoformat(),mount={'source':module.EXPECTED_NFS},container=identity,targets=rows)
        readiness={name:True for name in module.REQUIRED_READINESS};readiness.update(checked_at_utc=module.utc().isoformat(),remaining_taide_roles=[],title_llm_enabled=False,role_providers=dict({name:'terra' for name in module.TERRA_ROLES},title='deterministic'))
        return proc,listener,row,identity,plan,readiness

    def guards(self,proc,listener,identity):
        stack=contextlib.ExitStack()
        for name,value in [('process',proc),('listener_inventory',[listener]),('container_identity',identity),('managed_records',[]),('established_counts',{port:0 for port in module.SERVICES.values()}),('vllm_counts',{'num_requests_running':0,'num_requests_waiting':0})]:
            stack.enter_context(patch.object(module,name,return_value=value))
        return stack

    def test_only_fixed_legacy_class_matches_and_qwen_or_embedding_never_matches(self):
        proc,*_=self.fixture();self.assertTrue(module.classify('taide_gateway8000',proc))
        self.assertFalse(module.classify('../../arbitrary',proc))
        for forbidden in ['photo_service.py','api_server_embedding.py','qwen3-vl-4b-instruct']:
            altered=dict(proc,argv=proc['argv']+[forbidden]);self.assertFalse(module.classify('taide_gateway8000',altered))
        with self.assertRaises(ValueError):module.process(1)

    def test_mixed_listener_owners_must_all_resolve_to_same_approved_controller(self):
        proc,*_=self.fixture()
        foreign=dict(proc,pid=202,ppid=1,argv=['unapproved'])
        with patch.object(module,'process',side_effect=lambda pid:{101:proc,202:foreign}[pid]):
            with self.assertRaisesRegex(ValueError,'unapproved owner'):module.controller('taide_gateway8000',[101,202])
        worker=dict(foreign,ppid=101)
        with patch.object(module,'process',side_effect=lambda pid:{101:proc,202:worker}[pid]):
            self.assertEqual(module.controller('taide_gateway8000',[101,202])['pid'],101)

    def test_protected_listener_ancestor_is_not_a_retirement_target(self):
        proc,listener,row,*_=self.fixture()
        child=dict(proc,pid=202,ppid=101,argv=['photo_service.py'])
        listeners=[listener,dict(port=8027,pids=[202],local_address='127.0.0.1:8027')]
        with patch.object(module,'process',side_effect=lambda pid:{101:proc,202:child}[pid]):
            with self.assertRaisesRegex(ValueError,'ancestor'):module.verify_target_listener(row,listeners)

    def test_metrics_return_only_queue_counts_and_reject_ambiguous_values(self):
        text='vllm:num_requests_running{model_name="taide"} 0 123456\nvllm:num_requests_waiting 2\nprivate_prompt_info{value="must not retain"} 99\n'
        self.assertEqual(module.parse_vllm_metrics(text),{'num_requests_running':0,'num_requests_waiting':2})
        for value in ['NaN','-1','0.5']:
            with self.assertRaises(ValueError):module.parse_vllm_metrics('vllm:num_requests_running '+value+'\nvllm:num_requests_waiting 0')
        with self.assertRaisesRegex(ValueError,'unavailable'):module.parse_vllm_metrics('vllm:num_requests_running 0')

    def test_missing_dependency_migration_or_nonterra_role_refuses(self):
        *_,ready=self.fixture()
        self.assertTrue(module.verify_prerequisites(ready))
        for change in [dict(enable_taide_is_zero=False),dict(mac_legacy_router_disabled=False),dict(remaining_taide_roles=['title']),dict(role_providers={'chat':'taide'})]:
            with self.assertRaises(ValueError):module.verify_prerequisites(dict(ready,**change))

    def test_disabled_legacy_router_and_deterministic_title_are_supported(self):
        *_,ready=self.fixture()
        self.assertNotIn('mac_router_ready',module.REQUIRED_READINESS)
        self.assertTrue(module.verify_prerequisites(ready))
        enabled=dict(ready,title_llm_enabled=True,role_providers=dict(ready['role_providers'],title='terra'))
        self.assertTrue(module.verify_prerequisites(enabled))
        for changes in [dict(title_llm_enabled=True),dict(title_llm_enabled=0),dict(title_llm_enabled=None),
                        dict(role_providers=dict(ready['role_providers'],title='terra'))]:
            with self.assertRaises(ValueError):module.verify_prerequisites(dict(ready,**changes))

    def test_stale_or_reused_pid_blocks_all_signals_before_any_stop(self):
        proc,listener,row,identity,plan,ready=self.fixture()
        with self.guards(proc,listener,identity),patch.object(module,'stop_one') as stop:
            plan['targets'][2]['start_ticks']='old'
            with self.assertRaisesRegex(ValueError,'identity changed'):module.apply_plan(plan,ready)
            stop.assert_not_called()

    def test_protected_listener_owner_never_receives_legacy_signal(self):
        proc,listener,row,identity,plan,ready=self.fixture()
        with self.guards(proc,listener,identity),patch.object(module,'listener_inventory',return_value=[listener,dict(port=8025,pids=[101],local_address='127.0.0.1:8025')]),patch.object(module,'stop_one') as stop:
            with self.assertRaisesRegex(ValueError,'protected'):module.apply_plan(plan,ready)
            stop.assert_not_called()

    def test_busy_gateway_or_engine_refuses_before_any_signal(self):
        for service in ['taide_gateway8000','taide_engine8200']:
            proc,listener,row,identity,plan,ready=self.fixture(service)
            with self.guards(proc,listener,identity),patch.object(module,'stop_one') as stop:
                if service=='taide_engine8200':
                    with patch.object(module,'vllm_counts',return_value={'num_requests_running':1,'num_requests_waiting':0}):
                        with self.assertRaisesRegex(ValueError,'running or waiting'):module.apply_plan(plan,ready)
                else:
                    with patch.object(module,'established_counts',return_value={port:int(port==8000) for port in module.SERVICES.values()}):
                        with self.assertRaisesRegex(ValueError,'established'):module.apply_plan(plan,ready)
                stop.assert_not_called()

    def test_pidfd_is_opened_before_identity_check_and_pid_reuse_sends_no_signal(self):
        proc,_,row,*_=self.fixture()
        with patch.object(module.os,'pidfd_open',return_value=42,create=True),patch.object(module.os,'close') as close,patch.object(module.signal,'pidfd_send_signal',create=True) as send,patch.object(module,'process',return_value=dict(proc,start_ticks='45')):
            with self.assertRaisesRegex(ValueError,'identity changed'):module.stop_one(row)
            send.assert_not_called();close.assert_called_once_with(42)

    def test_graceful_stop_uses_only_pinned_sigterm_and_timeout_never_force_kills(self):
        proc,listener,row,*_=self.fixture()
        with patch.object(module.os,'pidfd_open',return_value=42,create=True),patch.object(module.os,'close'),patch.object(module.signal,'pidfd_send_signal',create=True) as send,patch.object(module,'process',side_effect=[proc,proc,FileNotFoundError()]),patch.object(module,'managed_records',return_value=[]),patch.object(module,'established_counts',return_value={8000:0}),patch.object(module,'listener_inventory',side_effect=[[listener],[]]):
            self.assertTrue(module.stop_one(row)['stopped']);send.assert_called_once_with(42,signal.SIGTERM,None,0)
        with patch.object(module.os,'pidfd_open',return_value=42,create=True),patch.object(module.os,'close'),patch.object(module.signal,'pidfd_send_signal',create=True) as send,patch.object(module,'process',return_value=proc),patch.object(module,'managed_records',return_value=[]),patch.object(module,'listener_inventory',return_value=[listener]),patch.object(module,'established_counts',return_value={8000:0}),patch.object(module.time,'monotonic',side_effect=[0,0,2]),patch.object(module.time,'sleep'):
            event={}
            with self.assertRaisesRegex(RuntimeError,'no SIGKILL'):module.stop_one(row,timeout=.01,event=event)
            self.assertTrue(event['signal_sent']);self.assertFalse(event['stopped'])
            send.assert_called_once_with(42,signal.SIGTERM,None,0)

    def test_new_listener_or_protected_ownership_before_signal_is_rejected(self):
        proc,listener,row,*_=self.fixture()
        for listeners in [[],[listener,dict(port=8000,pids=[202])],[listener,dict(port=8027,pids=[101])]]:
            with patch.object(module.os,'pidfd_open',return_value=42,create=True),patch.object(module.os,'close'),patch.object(module.signal,'pidfd_send_signal',create=True) as send,patch.object(module,'process',return_value=proc),patch.object(module,'listener_inventory',return_value=listeners):
                with self.assertRaises(ValueError):module.stop_one(row)
                send.assert_not_called()

    def test_audit_write_failure_before_signal_stops_retirement(self):
        proc,listener,row,identity,plan,ready=self.fixture()
        with self.guards(proc,listener,identity),patch.object(module,'stop_one') as stop:
            result=module.apply_plan(plan,ready,audit_callback=Mock(side_effect=OSError('audit not writable')))
            self.assertEqual(result['status'],'partial_or_refused');self.assertEqual(result['signals'],[]);stop.assert_not_called()

    def test_default_plan_is_read_only_and_does_not_retain_secret_args(self):
        proc,listener,row,identity,plan,ready=self.fixture()
        with self.guards(proc,listener,identity),patch.object(module,'require_mount',return_value=plan['mount']),patch.object(module.signal,'pidfd_send_signal',create=True) as send:
            result=module.build_plan();self.assertTrue(result['read_only']);send.assert_not_called()
        redacted=module.redact(['python','--api-key','secret-one','--hf-token=secret-two','http://user:secret-three@example.test'])
        self.assertNotIn('secret-',json.dumps(redacted))

if __name__=='__main__':unittest.main()
