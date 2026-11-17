"""Formal/candidate BGE safety checks; no real processes or GPU are touched."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bge_control', Path(__file__).with_name('manage-bge-model.py'))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
READY = dict(ready=True, status='ready', model='BAAI/bge-m3', revision=control.REVISION, dimension=1024, busy=False)


class BGEControl(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / 'src/secure'
        self.python = self.root / 'envs/secure/bin/python'
        self.source.mkdir(parents=True)
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.states = self.root / 'pids'
        self.states.mkdir()
        self.active = self.root / 'bge-model.active.json'
        self.record = dict(service='blink-managed-bge-v1', instance='bge-model-candidate', pid=123456, port=8027,
            start_ticks='1', source=str(self.source), python=str(self.python),
            command=[str(self.python), '-u', str(self.source / 'bge_secure_service.py'), '--port', '8027'])
        self.mount = dict(target=str(self.root), fstype='nfs4', source=control.EXPECTED_NFS, options='rw,relatime')
        self.metadata = dict(profile='secure-rocm71', python_lock_sha256='pure', native_lock_sha256='native')
        self.patches = [patch.object(control, 'ROOT', self.root), patch.object(control, 'STATE_DIR', self.states),
            patch.object(control, 'SOURCE', self.source), patch.object(control, 'PYTHON', self.python), patch.object(control, 'ACTIVE', self.active),
            patch.object(control.subprocess, 'check_output', return_value=json.dumps({'filesystems': [self.mount]})),
            patch.object(control.os, 'pidfd_open', return_value=42, create=True), patch.object(control.os, 'close'),
            patch.object(control.signal, 'pidfd_send_signal', create=True), patch.object(control.time, 'sleep')]
        self.mocks = [item.start() for item in self.patches]
        self.send = self.mocks[-2]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def invoke(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            return control.main(list(arguments))

    def write(self, formal=False, **changes):
        record = dict(self.record)
        if formal:
            record.update(instance='bge-model', port=8002,
                command=[str(self.python), '-u', str(self.source / 'bge_secure_service.py'), '--port', '8002'])
        record.update(changes)
        state = self.states / (record['instance'] + '.identity.json')
        state.write_text(json.dumps(record))
        return record, state

    def owner(self, record=None):
        record = record or self.record
        return [{'address': '127.0.0.1:' + str(record['port']), 'pids': [record['pid']]}]

    def active_value(self):
        return dict(instance='bge-model', port=8002, profile='secure-rocm71', source=str(self.source), python=str(self.python),
            source_manifest_sha256='source', python_lock_sha256='pure', native_lock_sha256='native')

    def test_wrong_mount_refuses_before_directory_start_or_signal(self):
        for changes in ({'target': '/'}, {'source': 'wrong-nfs'}, {'fstype': 'overlay'}, {'options': 'ro,relatime'}):
            missing = self.root / 'must-not-create'
            with self.subTest(changes=changes), patch.object(control, 'STATE_DIR', missing), patch.object(control.subprocess, 'check_output',
                    return_value=json.dumps({'filesystems': [dict(self.mount, **changes)]})), patch.object(control.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(SystemExit, 'NFS mount missing'):
                    self.invoke('stop')
                self.assertFalse(missing.exists())
                launch.assert_not_called()
                self.send.assert_not_called()

    def test_candidate_cannot_adopt_formal_identity_or_foreign_command(self):
        for changes in ({'port': 8002}, {'command': ['python', '-m', 'vllm.entrypoints.openai.api_server']}, {'pid': 1}):
            self.write(**changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(SystemExit, 'Foreign BGE'):
                self.invoke('stop')
        self.send.assert_not_called()

    def test_pid_reuse_after_pidfd_sends_no_signal(self):
        self.write()
        with patch.object(control, 'live', side_effect=[True, False]), self.assertRaisesRegex(SystemExit, 'changed after pidfd'):
            self.invoke('stop')
        self.send.assert_not_called()

    def test_busy_or_unready_service_is_not_signalled(self):
        self.write()
        for health in (dict(READY, busy=True), None, {'status': 'ready'}):
            with self.subTest(health=health), patch.object(control, 'live', return_value=True), patch.object(control, 'listeners', return_value=self.owner()), patch.object(control, 'health', return_value=health):
                with self.assertRaisesRegex(SystemExit, 'confirm BGE idle'):
                    self.invoke('stop')
        self.send.assert_not_called()

    def test_foreign_or_public_listener_is_never_signalled(self):
        self.write()
        for owners in ([{'address': '127.0.0.1:8027', 'pids': [999]}], [{'address': '0.0.0.0:8027', 'pids': [123456]}]):
            with self.subTest(owners=owners), patch.object(control, 'live', return_value=True), patch.object(control, 'listeners', return_value=owners):
                with self.assertRaisesRegex(SystemExit, 'listener owner/bind changed'):
                    self.invoke('stop')
        self.send.assert_not_called()

    def test_port_must_close_before_identity_is_removed(self):
        _, state = self.write()
        with patch.object(control, 'live', side_effect=[True, True, False, False]), patch.object(control, 'health', return_value=READY), patch.object(control, 'listeners', side_effect=[self.owner(), self.owner(), []]) as opened:
            self.invoke('stop')
        self.send.assert_called_once_with(42, control.signal.SIGTERM, None, 0)
        self.assertEqual(opened.call_count, 3)
        self.assertFalse(state.exists())

    def test_existing_start_still_requires_readiness(self):
        self.write()
        with patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'live', return_value=True), patch.object(control, 'wait_ready', side_effect=SystemExit('readiness failed')):
            with self.assertRaisesRegex(SystemExit, 'readiness failed'):
                self.invoke('start')

    def test_first_formal_start_requires_reviewed_activation(self):
        with patch.object(control.subprocess, 'Popen') as launch, self.assertRaisesRegex(SystemExit, 'reviewed --activate'):
            self.invoke('start', '--instance', 'bge-model')
        launch.assert_not_called()
        self.assertFalse(self.active.exists())

    def test_formal_migration_never_takes_over_old_8002(self):
        with patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'listeners', return_value=[{'address': '127.0.0.1:8002', 'pids': [999]}]), patch.object(control.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(SystemExit, 'port occupied'):
                self.invoke('start', '--instance', 'bge-model', '--activate', '--source', str(self.source), '--python', str(self.python))
        launch.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.active.exists())

    def test_cold_start_follows_valid_secure_active_pointer(self):
        self.write(formal=True)
        self.active.write_text(json.dumps(self.active_value()))
        with patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'live', return_value=True), patch.object(control, 'wait_ready', return_value=READY) as wait, patch.object(control.subprocess, 'Popen') as launch:
            self.assertEqual(self.invoke('start', '--instance', 'bge-model'), 0)
        wait.assert_called_once()
        launch.assert_not_called()

    def test_explicit_formal_promotion_persists_only_after_readiness(self):
        self.write(formal=True)
        with patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'live', return_value=True), patch.object(control, 'wait_ready', return_value=READY):
            self.assertEqual(self.invoke('start', '--instance', 'bge-model', '--activate', '--source', str(self.source), '--python', str(self.python)), 0)
        value = json.loads(self.active.read_text())
        self.assertEqual(value['instance'], 'bge-model')
        self.assertEqual(value['port'], 8002)
        self.assertEqual(value['source_manifest_sha256'], 'source')
        self.assertEqual(value['native_lock_sha256'], 'native')

    def test_failed_formal_readiness_does_not_write_active_pointer(self):
        self.write(formal=True)
        with patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'live', return_value=True), patch.object(control, 'wait_ready', side_effect=SystemExit('readiness failed')):
            with self.assertRaisesRegex(SystemExit, 'readiness failed'):
                self.invoke('start', '--instance', 'bge-model', '--activate', '--source', str(self.source), '--python', str(self.python))
        self.assertFalse(self.active.exists())

    def test_active_pointer_rejects_legacy_and_source_hash_drift(self):
        for changes in ({'profile': 'legacy-rocm64'}, {'source_manifest_sha256': 'changed'}):
            self.active.write_text(json.dumps(dict(self.active_value(), **changes)))
            with self.subTest(changes=changes), patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')):
                with self.assertRaises(SystemExit):
                    control.active_config()

    def test_status_http200_without_full_ready_schema_is_not_success(self):
        self.write()
        with patch.object(control, 'live', return_value=True), patch.object(control, 'verified_runtime', return_value=(self.metadata, {}, 'source')), patch.object(control, 'listeners', return_value=self.owner()), patch.object(control, 'health', return_value={'status': 'ready'}):
            self.assertEqual(self.invoke('status'), 1)

    def test_child_env_removes_inherited_python_and_loader_overrides(self):
        with patch.dict(control.os.environ, {key: 'foreign' for key in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH')}):
            value = control.clean_env()
        self.assertTrue(all(key not in value for key in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH')))
        self.assertEqual(value['PYTHONNOUSERSITE'], '1')

    def test_runtime_refuses_extra_source_files_or_changed_package_versions(self):
        required = ['bge_secure_service.py', 'bounded_http.py', 'bge-m3.secure.manifest.json']
        for name in required:
            (self.source / name).write_text('fixture')
        lock = self.source / 'requirements-bge-linux-py311.lock'
        shutil.copyfile(Path(__file__).resolve().parents[2] / 'experiments/skin-vlm/requirements-bge-linux-py311.lock', lock)
        metadata = dict(self.metadata, python_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest())
        (self.python.parent.parent / 'runtime-ready.json').write_text(json.dumps(metadata))
        manifest = {path.name: control.digest(path) for path in self.source.iterdir()}
        (self.source / 'source-manifest.json').write_text(json.dumps(manifest))
        versions = dict(control.NATIVE)
        for line in lock.read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                name, version = line.split()[0].split('==')
                versions[name] = version
        with patch.object(control.subprocess, 'check_output', side_effect=[json.dumps(versions), '/persistent/torch/lib']):
            self.assertEqual(control.verified_runtime(self.source, self.python)[0], metadata)
        (self.source / 'json.py').write_text('unreviewed override')
        with self.assertRaisesRegex(SystemExit, 'Unreviewed files'):
            control.verified_runtime(self.source, self.python)
        (self.source / 'json.py').unlink()
        with patch.object(control.subprocess, 'check_output', return_value=json.dumps(dict(versions, torch='2.8.0+rocm6.4'))), self.assertRaisesRegex(SystemExit, 'packages differ'):
            control.verified_runtime(self.source, self.python)


if __name__ == '__main__':
    unittest.main()
