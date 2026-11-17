"""Run real bootstrap/downloader shells against local, non-downloading stubs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class BootstrapSelection(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.runtime = self.root / 'ops/runtime'
        self.install = self.runtime / 'install'
        self.install.mkdir(parents=True)
        original = Path(__file__).parent
        for name in ['install_all.sh', 'runtime.conf']:
            shutil.copyfile(original / name, self.runtime / name)
        for name in ['50_download_models.sh', '40_vllm_env.sh']:
            shutil.copyfile(original / 'install' / name, self.install / name)
        for name in ['verify-mi300x-host.sh', 'verify-mi300x-mount.sh']:
            self.executable(self.runtime / name, '#!/bin/sh\nexit 0\n')
        for name in ['10_system_packages', '30_embedding_env']:
            self.executable(self.install / (name + '.sh'), '#!/bin/sh\nprintf "step:' + name + '\\n" >> "$TEST_LOG"\n')
        self.embed = self.root / 'envs/embed'
        self.vllm = self.root / 'envs/vllm'
        self.executable(self.embed / 'bin/python', '#!/bin/sh\nprintf "embed:%s\\n" "$*" >> "$TEST_LOG"\nexit 0\n')
        self.executable(self.vllm / 'bin/python', '#!/bin/sh\nprintf "vllm:%s\\n" "$*" >> "$TEST_LOG"\nexit 0\n')
        self.log = self.root / 'calls'
        self.env = dict(os.environ, ENABLE_TAIDE='0', GOODAGENTS_ROOT=str(self.root / 'persistent'),
                        EMBED_ENV=str(self.embed), VLLM_ENV=str(self.vllm), TEST_LOG=str(self.log), HF_TOKEN='public-test-placeholder')
        self.env.pop('REQUESTED_MODELS', None)

    def tearDown(self):
        self.directory.cleanup()

    def executable(self, target, content):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o755)

    def invoke(self, script, **changes):
        self.log.unlink(missing_ok=True)
        return subprocess.run(['/bin/bash', str(script)], env=dict(self.env, **changes), text=True,
                              capture_output=True, timeout=5)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_default_bootstrap_skips_step40_and_uses_only_embedding_python(self):
        # Any accidental step40 invocation must fail loudly without touching an environment.
        self.executable(self.install / '40_vllm_env.sh', '#!/bin/sh\nprintf "FORBIDDEN_STEP40\\n" >> "$TEST_LOG"\nexit 99\n')
        result = self.invoke(self.runtime / 'install_all.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn('step:10_system_packages', calls)
        self.assertIn('step:30_embedding_env', calls)
        self.assertIn('embed:', calls)
        self.assertIn('--models embedding', calls)
        self.assertNotIn('FORBIDDEN_STEP40', calls)
        self.assertNotIn('vllm:', calls)
        self.assertNotIn('--models taide', calls)
        self.assertNotIn(' pip ', calls)
        result = self.invoke(self.install / '50_download_models.sh', EMBED_ENV=str(self.vllm))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不能使用舊 vLLM', result.stderr)
        self.assertEqual(self.calls(), '')

    def test_disabled_explicit_taide_refuses_before_any_partial_model_work(self):
        result = self.invoke(self.install / '50_download_models.sh', REQUESTED_MODELS='embedding,taide-prod')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ENABLE_TAIDE=1', result.stderr)
        self.assertEqual(self.calls(), '')

    def test_explicit_taide_requires_flag_and_its_own_vllm_python(self):
        result = self.invoke(self.install / '50_download_models.sh', ENABLE_TAIDE='1', REQUESTED_MODELS='taide-prod')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('vllm:', self.calls())
        self.assertIn('--models taide-prod', self.calls())
        self.assertNotIn('embed:', self.calls())
        (self.vllm / 'bin/python').unlink()
        result = self.invoke(self.install / '50_download_models.sh', ENABLE_TAIDE='1', REQUESTED_MODELS='taide-prod')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), '')

    def test_missing_legacy_gateway_dependency_never_triggers_pip_mutation(self):
        self.executable(self.vllm / 'bin/python', '#!/bin/sh\nprintf "vllm:%s\\n" "$*" >> "$TEST_LOG"\nexit 7\n')
        result = self.invoke(self.install / '40_vllm_env.sh', ENABLE_TAIDE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不自動 pip', result.stderr)
        self.assertNotIn('-m pip', self.calls())


if __name__ == '__main__':
    unittest.main()
