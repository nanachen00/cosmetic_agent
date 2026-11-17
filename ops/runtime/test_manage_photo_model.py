"""Control-plane regression tests; no real processes or GPU services touched."""
import contextlib,importlib.util,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('photo_control_test_target',Path(__file__).with_name('manage-photo-model.py'))
control=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(control)


class PhotoControl(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name).resolve()
        self.source=self.root/'src/secure';self.python=self.root/'envs/secure/bin/python'
        self.state=self.root/'pids/photo-model.identity.json';self.active=self.root/'photo-model.active.json'
        self.state.parent.mkdir();self.source.mkdir(parents=True);self.python.parent.mkdir(parents=True);self.python.touch()
        self.record=dict(pid=123456,port=8025,start_ticks='1',source=str(self.source),python=str(self.python),
            command=[str(self.python),'-u',str(self.source/'photo_service.py')])
        self.patches=[patch.object(control,'ROOT',self.root),patch.object(control,'STATE',self.state),
            patch.object(control,'ACTIVE',self.active),patch.object(control,'DEFAULT_SOURCE',self.source),patch.object(control,'DEFAULT_PYTHON',self.python),
            patch.object(control.subprocess,'check_output',return_value=json.dumps({'filesystems':[{'target':str(self.root),'fstype':'nfs4','source':control.EXPECTED_NFS,'options':'rw,relatime'}]})),
            patch.object(control.os,'kill')]
        self.mocks=[p.start() for p in self.patches]
        self.pidfd_patches=[patch.object(control.os,'pidfd_open',return_value=42,create=True),
            patch.object(control.signal,'pidfd_send_signal',create=True),patch.object(control.os,'close')]
        self.pidfd_open,self.pidfd_send,self.pidfd_close=[p.start() for p in self.pidfd_patches]
    def tearDown(self):
        for p in reversed(self.pidfd_patches):p.stop()
        for p in reversed(self.patches):p.stop()
        self.directory.cleanup()
    def invoke(self,*args):
        with patch('sys.argv',['manage-photo-model.py',*args]),contextlib.redirect_stdout(io.StringIO()):control.main()
    def write(self,record=None,path=None):
        (path or self.state).write_text(json.dumps(record or self.record))

    def test_foreign_namespace_is_rejected_before_any_signal(self):
        foreign=self.state.with_name('taide.identity.json');self.write(path=foreign)
        with self.assertRaisesRegex(SystemExit,'instance name'):self.invoke('stop','--instance','taide')
        self.assertTrue(foreign.exists());self.mocks[-1].assert_not_called()

    def test_wrong_or_read_only_volume_is_rejected_before_control(self):
        for changes in [{'source':'other:/volume'},{'options':'ro,relatime'},{'fstype':'ext4'}]:
            mount=dict(target=str(self.root),fstype='nfs4',source=control.EXPECTED_NFS,options='rw,relatime')
            mount.update(changes)
            with self.subTest(changes=changes),patch.object(control.subprocess,'check_output',return_value=json.dumps({'filesystems':[mount]})):
                with self.assertRaisesRegex(SystemExit,'writable school NFS'):self.invoke('stop')
            self.mocks[-1].assert_not_called()
            self.assertFalse(self.state.with_suffix('.lock').exists())

    def test_foreign_command_in_photo_namespace_is_rejected(self):
        record=dict(self.record,command=['python','-m','vllm.entrypoints.openai.api_server'])
        self.write(record)
        with patch.object(control,'live',return_value=True),self.assertRaisesRegex(SystemExit,'not an isolated photo'):
            self.invoke('stop')
        self.assertTrue(self.state.exists());self.mocks[-1].assert_not_called()

    def test_default_existing_service_uses_secure_configuration(self):
        self.write()
        with patch.object(control,'live',return_value=True),patch.object(control,'health',return_value={'ready':True,'failed':False}):
            self.invoke('start')
        self.mocks[-1].assert_not_called()

    def test_persistent_active_configuration_is_used(self):
        source=self.root/'src/active';python=self.root/'envs/active/bin/python'
        source.mkdir();python.parent.mkdir(parents=True);python.touch()
        self.active.write_text(json.dumps({'source':str(source),'python':str(python),'profile':'secure-rocm71'}))
        record=dict(self.record,source=str(source),python=str(python),command=[str(python),'-u',str(source/'photo_service.py')]);self.write(record)
        with patch.object(control,'live',return_value=True),patch.object(control,'health',return_value={'ready':True,'failed':False}):
            self.invoke('start')

    def test_failed_existing_service_cannot_report_success(self):
        self.write()
        with patch.object(control,'live',return_value=True),patch.object(control,'health',return_value={'ready':False,'failed':True}),self.assertRaisesRegex(SystemExit,'failed to load'):
            self.invoke('start')

    def test_pid_identity_change_cannot_be_signalled(self):
        self.write()
        with patch.object(control,'live',return_value=False),self.assertRaisesRegex(SystemExit,'identity differs'):
            self.invoke('stop')
        self.mocks[-1].assert_not_called()
        self.pidfd_open.assert_not_called();self.pidfd_send.assert_not_called()

    def test_pid_reuse_after_descriptor_open_cannot_be_signalled(self):
        self.write()
        with patch.object(control,'live',side_effect=[True,False]),self.assertRaisesRegex(SystemExit,'after pidfd acquisition'):
            self.invoke('stop')
        self.pidfd_open.assert_called_once_with(self.record['pid'],0)
        self.pidfd_close.assert_called_once_with(42);self.pidfd_send.assert_not_called();self.mocks[-1].assert_not_called()
        self.assertTrue(self.state.exists())

    def test_missing_pidfd_capability_refuses_numeric_fallback(self):
        self.write()
        for owner,name in [(control.os,'pidfd_open'),(control.signal,'pidfd_send_signal')]:
            saved=getattr(owner,name);delattr(owner,name)
            try:
                with patch.object(control,'live',return_value=True),self.assertRaisesRegex(SystemExit,'pidfd support required'):
                    self.invoke('stop')
            finally:setattr(owner,name,saved)
        self.pidfd_send.assert_not_called();self.mocks[-1].assert_not_called();self.assertTrue(self.state.exists())

    def test_foreign_wildcard_mixed_or_missing_listener_owner_refuses_signal(self):
        self.write()
        for output in ['', 'LISTEN 0 128 0.0.0.0:8025 0.0.0.0:* users:(("python",pid=123456,fd=7))',
                       'LISTEN 0 128 127.0.0.1:8025 0.0.0.0:* users:(("python",pid=202,fd=7))',
                       'LISTEN 0 128 127.0.0.1:8025 0.0.0.0:* users:(("python",pid=123456,fd=7),("python",pid=202,fd=8))']:
            with patch.object(control,'live',return_value=True),patch.object(control.subprocess,'check_output',return_value=output),self.assertRaisesRegex(SystemExit,'listener owner differs'):
                control.stop(self.record,self.state)
        self.pidfd_send.assert_not_called();self.mocks[-1].assert_not_called();self.assertTrue(self.state.exists())

    def test_busy_or_unavailable_health_cannot_confirm_idle(self):
        self.write()
        for health in [None,{},dict(busy=True),dict(busy=0)]:
            with patch.object(control,'live',return_value=True),patch.object(control,'owns_loopback',return_value=True),patch.object(control,'health',return_value=health),self.assertRaisesRegex(SystemExit,'confirm photo inference is idle'):
                self.invoke('stop')
        self.pidfd_send.assert_not_called();self.mocks[-1].assert_not_called();self.assertTrue(self.state.exists())

    def test_single_exact_loopback_owner_is_accepted(self):
        output='LISTEN 0 128 127.0.0.1:8025 0.0.0.0:* users:(("python",pid=123456,fd=7))'
        with patch.object(control.subprocess,'check_output',return_value=output):
            self.assertTrue(control.owns_loopback(self.record))

    def test_replacement_listener_after_exit_keeps_identity_and_never_signals_again(self):
        self.write()
        with patch.object(control,'live',side_effect=[True,True,*([False]*51)]),patch.object(control,'owns_loopback',return_value=True),patch.object(control,'health',return_value={'busy':False}),patch.object(control,'port_open',return_value=True),patch.object(control.time,'sleep'),self.assertRaisesRegex(SystemExit,'port remains occupied'):
            self.invoke('stop')
        self.pidfd_send.assert_called_once_with(42,control.signal.SIGTERM,None,0)
        self.mocks[-1].assert_not_called();self.assertTrue(self.state.exists())

    def test_stop_waits_for_socket_release_before_removing_identity(self):
        self.write()
        with patch.object(control,'live',side_effect=[True,True,False,False,False]),patch.object(control,'owns_loopback',return_value=True),patch.object(control,'health',return_value={'busy':False}),patch.object(control,'port_open',side_effect=[True,False,False]) as opened,patch.object(control.time,'sleep') as sleep:
            self.invoke('stop')
        self.pidfd_send.assert_called_once_with(42,control.signal.SIGTERM,None,0)
        self.pidfd_open.assert_called_once_with(self.record['pid'],0);self.pidfd_close.assert_called_once_with(42)
        self.mocks[-1].assert_not_called()
        self.assertEqual(opened.call_count,3);sleep.assert_called_once_with(.1)
        self.assertFalse(self.state.exists())

    def test_active_pointer_rejects_legacy_profile(self):
        ready=self.python.parent.parent/'runtime-ready.json';ready.write_text(json.dumps({'profile':'legacy-rocm64'}))
        self.write()
        with patch.object(control,'live',return_value=True),patch.object(control,'health',return_value={'ready':True,'failed':False}),self.assertRaisesRegex(SystemExit,'secure runtime profile'):
            self.invoke('start','--activate')
        self.assertFalse(self.active.exists())


if __name__=='__main__':unittest.main()
