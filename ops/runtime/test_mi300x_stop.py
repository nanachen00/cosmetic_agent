"""Exercise the actual stop script with harmless process-control stubs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class StopRuntime(unittest.TestCase):
    def test_mi300x_stop_never_signals_or_removes_legacy_pidfiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'ops/runtime'
            runtime.mkdir(parents=True)
            original = Path(__file__).with_name('stop-production.sh')
            shutil.copyfile(original, runtime / original.name)
            pids = root / 'pids'
            pids.mkdir()
            legacy = ['embedding', 'reranker', 'gateway-8000', 'vllm-ab', 'vllm-prod']
            for name in legacy:
                (pids / (name + '.pid')).write_text('123456\n')
            shutil.copyfile(Path(__file__).with_name('runtime.conf'), runtime / 'runtime.conf')
            verifier = runtime / 'verify-mi300x-mount.sh'
            verifier.write_text('#!/bin/sh\nexit 0\n')
            verifier.chmod(0o755)
            commands = root / 'commands'
            commands.mkdir()
            python = commands / 'python3'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\n')
            python.chmod(0o755)
            calls = root / 'calls'
            signals = root / 'signals'
            startup = root / 'bash-startup'
            startup.write_text('kill() { printf "%s\\n" "$*" >> "$TEST_SIGNALS"; return 1; }\n')
            env = dict(os.environ, BLINK_RUNTIME_ROLE='mi300x', PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       MI300X_CONTROL_PYTHON=str(python), PIDDIR=str(pids),
                       BASH_ENV=str(startup), TEST_CALLS=str(calls), TEST_SIGNALS=str(signals))
            result = subprocess.run(['/bin/bash', str(runtime / original.name)], env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('manage-bge-model.py stop --instance bge-model', calls.read_text())
            self.assertFalse(signals.exists(), 'No legacy numeric PID may reach even the stubbed kill builtin')
            for name in legacy:
                self.assertEqual((pids / (name + '.pid')).read_text(), '123456\n')
                self.assertIn('[SKIP] legacy ' + name, result.stdout)

    def test_missing_pidfd_control_python_blocks_all_mi_control_before_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'ops/runtime'
            runtime.mkdir(parents=True)
            for name in ['runtime.conf', 'start-mi300x-models.sh', 'status-production.sh', 'stop-production.sh']:
                shutil.copyfile(Path(__file__).with_name(name), runtime / name)
            control = root / 'control-python-without-pidfd'
            control.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\nexit 1\n')
            control.chmod(0o755)
            calls = root / 'calls'
            env = dict(os.environ, BLINK_RUNTIME_ROLE='mi300x', MI300X_CONTROL_PYTHON=str(control),
                       OUTPUT_DIR=str(root / 'must-not-create'), TEST_CALLS=str(calls))
            for name in ['start-mi300x-models.sh', 'status-production.sh', 'stop-production.sh']:
                calls.unlink(missing_ok=True)
                result = subprocess.run(['/bin/bash', str(runtime / name)], env=env, text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn('pidfd', result.stderr)
                self.assertIn('pidfd_send_signal', calls.read_text())
                self.assertNotIn('manage-bge-model.py', calls.read_text())
                self.assertNotIn('manage-photo-model.py', calls.read_text())
                self.assertFalse((root / 'must-not-create').exists())


if __name__ == '__main__':
    unittest.main()
