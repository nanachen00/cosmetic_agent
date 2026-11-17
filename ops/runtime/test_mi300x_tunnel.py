"""Offline SSH safety and recovery checks; no network/authentication attempts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('mi300x_tunnel', Path(__file__).with_name('mi300x-tunnel.py'))
tunnel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tunnel)


def completed(code=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def attestation():
    return dict(os='Linux', hostname='synthetic-school-host', kfd=True, mi300x_verified=True,
        mount=dict(target='/mnt/goodagents', source=tunnel.NFS_SOURCE, fstype='nfs4', options='rw,relatime,vers=4.2', writable=True),
        listeners={str(port): ['127.0.0.1:' + str(port)] for port in tunnel.PORTS},
        services={str(port): {'ready': True, 'busy': False, 'failed': False} for port in tunnel.PORTS})


class TunnelTests(unittest.TestCase):
    def test_missing_socket_fails_before_any_ssh_even_for_recover(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tunnel, 'run', side_effect=AssertionError('must not login')), \
                patch.object(tunnel, 'local_tcp_open', return_value=False):
            result, code = tunnel.diagnose(Path(directory) / 'missing', recover=True)
        self.assertEqual(code, 3)
        self.assertEqual(result['reason'], 'control_socket_missing')
        self.assertFalse(result['new_login_attempted'])
        self.assertFalse(result['recovery_performed'])
        self.assertFalse(result['models_or_services_restarted'])
        self.assertEqual(result['remote_model_and_research_state'], 'unverified')

    def test_regular_file_or_insecure_socket_is_not_an_authenticated_master(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control'
            path.touch()
            with patch.object(tunnel, 'run', side_effect=AssertionError('must not use a file')):
                with self.assertRaisesRegex(tunnel.TunnelError, 'private_owned_socket'):
                    tunnel.verified_master(path)
            path.unlink()
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path));path.chmod(0o666)
                with self.assertRaisesRegex(tunnel.TunnelError, 'private_owned_socket'):
                    tunnel.verified_master(path)

    def test_mux_clients_cannot_read_keys_prompt_or_fallback_to_new_connection(self):
        argv = tunnel.mux_args(Path('/tmp/test-control'))
        for required in ('BatchMode=yes', 'NumberOfPasswordPrompts=0', 'PasswordAuthentication=no',
                'KbdInteractiveAuthentication=no', 'PubkeyAuthentication=no', 'IdentityFile=none',
                'CertificateFile=none', 'IdentityAgent=none', 'StrictHostKeyChecking=yes',
                'ControlMaster=no', 'ProxyCommand=/usr/bin/false'):
            self.assertIn(required, argv)
        self.assertEqual(argv[argv.index('-F') + 1], '/dev/null')
        self.assertNotIn('-i', argv)

    def test_only_the_explicit_authorized_nonconfirming_master_tuple_is_accepted(self):
        good = [
            'ssh -M -S /tmp/control -NT4 -p 30892 root@140.119.24.206',
            '/usr/bin/ssh -M -S/tmp/control -N -T -p30892 root@140.119.24.206',
            'ssh -oControlMaster=yes -oPort=30892 -lroot 140.119.24.206',
        ]
        for command in good:
            with self.subTest(command=command):self.assertTrue(tunnel.explicit_master_target(command))
        bad = [
            'ssh -M -p22 root@140.119.24.206', 'ssh -M -p30892 root@elsewhere.example',
            'ssh -M -p30892 user@140.119.24.206', 'ssh -M -p30892 http://140.119.24.206:31484/lab',
            'ssh -MM -p30892 root@140.119.24.206', 'ssh -oControlMaster=ask -p30892 root@140.119.24.206',
            'ssh -M -oControlMaster=autoask -p30892 root@140.119.24.206',
            'ssh -M -oHostName=elsewhere.example -p30892 root@140.119.24.206',
            'ssh -M -Jother-host -p30892 root@140.119.24.206',
            'ssh -p30892 root@140.119.24.206',  # Unknown config might be ask mode.
        ]
        for command in bad:
            with self.subTest(command=command):self.assertFalse(tunnel.explicit_master_target(command))

    def fake_master(self, command=None, peer=None):
        command = command or 'ssh -M -S /tmp/control -NT4 -p30892 root@140.119.24.206'
        peer = peer or 'n127.0.0.1:51000->140.119.24.206:30892\n'
        def run(argv, **kwargs):
            if '-O' in argv:return completed(stderr='Master running (pid=123)\n')
            if argv[0] == tunnel.PS and 'uid=,comm=' in argv:return completed(stdout=str(os.getuid()) + ' /usr/bin/ssh\n')
            if argv[0] == tunnel.PS:return completed(stdout=command)
            return completed(stdout=peer)
        return run

    def test_authenticated_socket_must_match_process_uid_command_and_actual_tcp_peer(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as sock:
            path = Path(directory) / 'control';sock.bind(str(path));path.chmod(0o600)
            with patch.object(tunnel, 'lsof_path', return_value='/usr/sbin/lsof'), \
                    patch.object(tunnel, 'run', side_effect=self.fake_master()):
                self.assertEqual(tunnel.verified_master(path), 123)
            with patch.object(tunnel, 'lsof_path', return_value='/usr/sbin/lsof'), \
                    patch.object(tunnel, 'run', side_effect=self.fake_master(peer='n127.0.0.1:51000->192.0.2.1:30892\n')):
                with self.assertRaisesRegex(tunnel.TunnelError, 'tcp_peer_unverified'):
                    tunnel.verified_master(path)
            with patch.object(tunnel, 'run', return_value=completed(255, stderr='stale socket')) as run:
                with self.assertRaisesRegex(tunnel.TunnelError, 'master_unavailable'):
                    tunnel.verified_master(path)
                self.assertEqual(run.call_count, 1)
                self.assertTrue(path.exists(), 'Never delete a stale operator socket')

    def test_attestation_requires_actual_MI300X_and_exact_writable_NFS(self):
        self.assertEqual(tunnel.validate_remote(attestation())['os'], 'Linux')
        changes = [lambda x:x.update(mi300x_verified=False), lambda x:x.update(kfd=False),
            lambda x:x['mount'].update(target='/'), lambda x:x['mount'].update(fstype='ext4'),
            lambda x:x['mount'].update(source='different-nfs:/volume'), lambda x:x['mount'].update(options='ro,relatime'),
            lambda x:x['mount'].update(writable=False), lambda x:x.update(os='Darwin')]
        for change in changes:
            value = attestation();change(value)
            with self.subTest(change=change), self.assertRaises(tunnel.TunnelError):
                tunnel.validate_remote(value)
        self.assertNotIn('gfx942', tunnel.REMOTE, 'Hardware family alone must not attest MI300X')

    def test_public_remote_bind_or_failed_model_never_passes_attestation(self):
        for changes in (lambda x:x['listeners']['8025'].append('0.0.0.0:8025'),
                        lambda x:x['listeners'].update({'8002':['[::]:8002']}),
                        lambda x:x['services']['8002'].update(ready=False),
                        lambda x:x['services']['8025'].update(failed=True)):
            value=attestation();changes(value)
            with self.subTest(changes=changes), self.assertRaises(tunnel.TunnelError):tunnel.validate_remote(value)

    def test_remote_attestation_uses_only_readonly_constant_script_and_no_extra_forwards(self):
        with patch.object(tunnel, 'run', return_value=completed(stdout=json.dumps(attestation()))) as run:
            tunnel.remote_attestation(Path('/tmp/control'))
        argv=run.call_args.args[0]
        self.assertIn('ClearAllForwardings=yes', argv)
        self.assertEqual(argv[-3:], [tunnel.TARGET, '/usr/bin/python3', '-'])
        self.assertEqual(run.call_args.kwargs['data'], tunnel.REMOTE)
        self.assertNotIn('mkdir', tunnel.REMOTE)
        self.assertNotIn('kill(', tunnel.REMOTE)

    def test_invalid_remote_schema_and_banner_fail_explicitly(self):
        for value in ([], {}, {'mount':[]}, dict(attestation(), listeners=[])):
            with self.subTest(value=value), self.assertRaises(tunnel.TunnelError):tunnel.validate_remote(value)
        with patch.object(tunnel, 'run', return_value=completed(stdout='unexpected banner')):
            with self.assertRaisesRegex(tunnel.TunnelError, 'invalid_remote'):tunnel.remote_attestation(Path('/tmp/control'))

    def guards(self):
        return patch.multiple(tunnel, verified_master=lambda path:123,
            remote_attestation=lambda path:attestation(), local_tcp_open=lambda port:False,
            local_health=lambda port:True)

    def test_readonly_status_does_not_create_missing_forwards(self):
        with self.guards(), patch.object(tunnel, 'listeners', return_value=[]), \
                patch.object(tunnel, 'run', side_effect=AssertionError('status cannot mutate')):
            result, code=tunnel.diagnose()
        self.assertEqual(code,5);self.assertFalse(result['ready']);self.assertFalse(result['recovery_performed'])

    def test_explicit_recover_adds_only_two_loopback_forwards_to_same_master(self):
        counts={8002:0,8025:0}
        def listeners(port):
            counts[port]+=1
            return [] if counts[port]==1 else [{'pid':123,'address':'127.0.0.1:'+str(port)}]
        with self.guards(),patch.object(tunnel,'listeners',side_effect=listeners), \
                patch.object(tunnel,'run',return_value=completed()) as run:
            result,code=tunnel.diagnose(recover=True)
        self.assertEqual(code,0);self.assertTrue(result['ready']);self.assertTrue(result['recovery_performed'])
        self.assertFalse(result['models_or_services_restarted'])
        argv=run.call_args.args[0]
        self.assertEqual(argv[argv.index('-O')+1],'forward')
        self.assertEqual([argv[index+1] for index,value in enumerate(argv) if value=='-L'],
            ['127.0.0.1:8002:127.0.0.1:8002','127.0.0.1:8025:127.0.0.1:8025'])
        self.assertNotIn('-f',argv);self.assertNotIn('-R',argv)

    def test_existing_private_master_forwards_are_idempotent(self):
        with self.guards(),patch.object(tunnel,'listeners',side_effect=lambda port:[{'pid':123,'address':'127.0.0.1:'+str(port)}]), \
                patch.object(tunnel,'run',side_effect=AssertionError('nothing to create')):
            result,code=tunnel.diagnose(recover=True)
        self.assertEqual(code,0);self.assertFalse(result['recovery_performed'])

    def test_foreign_owner_or_public_local_bind_is_never_adopted_or_cancelled(self):
        for binding in ({'pid':456,'address':'127.0.0.1:8002'}, {'pid':123,'address':'*:8002'}):
            with self.subTest(binding=binding),self.guards(),patch.object(tunnel,'listeners',return_value=[binding]), \
                    patch.object(tunnel,'run',side_effect=AssertionError('cannot mutate foreign owner')):
                result,code=tunnel.diagnose(recover=True)
            self.assertEqual(code,5);self.assertEqual(result['reason'],'local_port_has_foreign_owner_or_non_loopback_binding')

    def test_forward_failure_reports_possible_partial_state_without_cancelling_connections(self):
        with self.guards(),patch.object(tunnel,'listeners',return_value=[]),patch.object(tunnel,'run',return_value=completed(255)) as run:
            result,code=tunnel.diagnose(recover=True)
        self.assertEqual(code,5);self.assertFalse(result['ready']);self.assertTrue(result['recovery_may_be_partial'])
        self.assertEqual(run.call_count,1);self.assertNotIn('cancel',run.call_args.args[0])


if __name__ == '__main__':unittest.main()
