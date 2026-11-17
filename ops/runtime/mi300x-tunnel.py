#!/usr/bin/env python3
"""Diagnose/reuse an authenticated school SSH master; never bootstrap a login.

Default status is read-only. Explicit recover adds only the missing 127.0.0.1
8002/8025 forwards to that same verified master. No passwords, keys, env files,
SSH configuration edits, GPU signals, mkdir on the server or service restarts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import urllib.request

HOST, USER, SSH_PORT = '140.119.24.206', 'root', 30892
TARGET = USER + '@' + HOST
CONTROL = Path('/tmp/blink-mi300x-control')
PORTS = (8002, 8025)
NFS_SOURCE = '192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
SSH, PS = '/usr/bin/ssh', '/bin/ps'

REMOTE = r'''
import json,os,re,shutil,socket,subprocess,urllib.request
from pathlib import Path
result={'os':os.uname().sysname,'hostname':socket.gethostname(),'kfd':Path('/dev/kfd').exists()}
gpu=False
for name,args in [('amd-smi',['static','--asic']),('rocminfo',[])]:
 executable=shutil.which(name) or ('/opt/rocm/bin/'+name if Path('/opt/rocm/bin/'+name).is_file() else None)
 if executable:
  try:
   p=subprocess.run([executable,*args],capture_output=True,text=True,timeout=12)
   if p.returncode==0 and re.search(r'\bMI300X\b',p.stdout,re.I):gpu=True;break
  except (OSError,subprocess.TimeoutExpired):pass
result['mi300x_verified']=gpu
mount=json.loads(subprocess.check_output(['/usr/bin/findmnt','-J','-T','/mnt/goodagents'],text=True,timeout=5))['filesystems'][0]
result['mount']={key:mount.get(key) for key in ('target','source','fstype','options')}
result['mount']['writable']=os.access('/mnt/goodagents',os.W_OK)
result['listeners']={str(port):[] for port in (8002,8025)}
for line in subprocess.check_output(['ss','-H','-ltn'],text=True,timeout=5).splitlines():
 fields=line.split()
 if len(fields)>3:
  for port in (8002,8025):
   if fields[3].endswith(':'+str(port)):result['listeners'][str(port)].append(fields[3])
result['services']={}
for port,path in [(8002,'/healthz'),(8025,'/health')]:
 try:
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:'+str(port)+path,timeout=3) as response:
   raw=response.read(4097)
   if response.status!=200 or len(raw)>4096:raise ValueError('Invalid health response')
   health=json.loads(raw)
  result['services'][str(port)]={key:health[key] for key in ('ready','status','busy','failed') if key in health}
 except Exception as exc:result['services'][str(port)]={'error_type':type(exc).__name__}
print(json.dumps(result))
'''


class TunnelError(RuntimeError):
    def __init__(self, reason, code=3):
        super().__init__(reason)
        self.reason, self.code = reason, code


def run(argv, *, data=None, timeout=8):
    try:
        return subprocess.run(argv, input=data, stdin=subprocess.DEVNULL if data is None else None,
            text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TunnelError('diagnostic_command_' + type(exc).__name__) from exc


def lsof_path():
    for value in ('/usr/sbin/lsof', '/usr/bin/lsof'):
        if Path(value).is_file():
            return value
    raise TunnelError('lsof_required_to_verify_listener_owner')


def mux_args(control):
    # An absent/dead -S normally falls back to a fresh connection. A deliberately
    # failing ProxyCommand prevents that race, while an existing mux still works.
    # No identity, certificate, agent or user's SSH config is loaded by clients.
    return [SSH, '-F', '/dev/null', '-4', '-T', '-S', str(control), '-p', str(SSH_PORT),
        '-o', 'BatchMode=yes', '-o', 'NumberOfPasswordPrompts=0',
        '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
        '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=none',
        '-o', 'IdentityFile=none', '-o', 'CertificateFile=none', '-o', 'IdentityAgent=none',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ControlMaster=no',
        '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
        '-o', 'ProxyCommand=/usr/bin/false']


def explicit_master_target(command):
    """Inspect the already running ssh argv without reporting sensitive options."""
    args = shlex.split(command)
    if not args or Path(args[0]).name != 'ssh':
        return False
    port, user, hostname, destination = None, None, None, None
    master_mode, master_flags = None, 0
    takes_value = set('BbcDEeFIiJLlmOopPQRSWw')
    index = 1
    while index < len(args):
        value = args[index]
        if value == '--':
            destination = args[index + 1] if index + 1 < len(args) else None
            break
        if not value.startswith('-'):
            destination = value
            break
        letters = value[1:]
        while letters:
            option, letters = letters[0], letters[1:]
            if option == 'M':
                master_flags += 1
            if option not in takes_value:
                continue
            if letters:
                argument, letters = letters, ''
            else:
                index += 1
                if index >= len(args):
                    return False
                argument = args[index]
            if option == 'p':
                port = argument
            elif option == 'l':
                user = argument
            elif option == 'J':
                return False
            elif option == 'o':
                parts = re.split(r'[=\s]+', argument, maxsplit=1)
                if len(parts) == 2:
                    key, item = parts[0].lower(), parts[1]
                    if key == 'port':port = item
                    elif key == 'user':user = item
                    elif key == 'hostname':hostname = item
                    elif key == 'controlmaster':master_mode = item.lower()
                    elif key in {'proxyjump', 'proxycommand'} and item != 'none':return False
            break
        index += 1
    # A master in ask/autoask or -MM mode can launch ssh-askpass despite a
    # client's BatchMode. Require an explicit non-confirming master mode.
    noninteractive = master_flags == 1 or master_mode in {'yes', 'auto'}
    return (noninteractive and master_flags <= 1 and master_mode not in {'ask', 'autoask'}
        and destination in {TARGET, HOST} and (destination == TARGET or user == USER)
        and user in {None, USER} and hostname in {None, HOST} and port == str(SSH_PORT))


def verified_master(control):
    try:
        info = control.lstat()
    except FileNotFoundError as exc:
        raise TunnelError('control_socket_missing') from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TunnelError('control_socket_not_private_owned_socket')
    check = run(mux_args(control) + ['-O', 'check', TARGET])
    match = re.search(r'Master running \(pid=(\d+)\)', check.stdout + check.stderr)
    if check.returncode or not match:
        raise TunnelError('authenticated_control_master_unavailable')
    pid = int(match.group(1))
    identity = run([PS, '-p', str(pid), '-o', 'uid=,comm='])
    fields = identity.stdout.strip().split(None, 1)
    if identity.returncode or len(fields) != 2 or fields[0] != str(os.getuid()) or Path(fields[1]).name != 'ssh':
        raise TunnelError('control_master_process_owner_unverified')
    command = run([PS, '-ww', '-p', str(pid), '-o', 'args='])
    if command.returncode or not explicit_master_target(command.stdout):
        raise TunnelError('control_master_target_not_explicitly_authorized')
    peer = run([lsof_path(), '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:ESTABLISHED', '-Fn'])
    endpoints = [line[1:].rsplit('->', 1)[-1] for line in peer.stdout.splitlines() if line.startswith('n') and '->' in line]
    if peer.returncode or HOST + ':' + str(SSH_PORT) not in endpoints:
        raise TunnelError('control_master_tcp_peer_unverified')
    return pid


def validate_remote(value):
    if not isinstance(value, dict) or not isinstance(value.get('mount'), dict):
        raise TunnelError('invalid_remote_attestation_schema', 4)
    mount = value.get('mount', {})
    if (value.get('os') != 'Linux' or value.get('kfd') is not True or value.get('mi300x_verified') is not True
            or mount.get('target') != '/mnt/goodagents' or mount.get('source') != NFS_SOURCE
            or mount.get('fstype') not in {'nfs', 'nfs4'} or not isinstance(mount.get('options'), str)
            or 'rw' not in mount['options'].split(',')
            or mount.get('writable') is not True):
        raise TunnelError('remote_MI300X_or_exact_writable_NFS_attestation_failed', 4)
    if not isinstance(value.get('listeners'), dict) or not isinstance(value.get('services'), dict):
        raise TunnelError('invalid_remote_attestation_schema', 4)
    for port in PORTS:
        if value['listeners'].get(str(port)) != ['127.0.0.1:' + str(port)]:
            raise TunnelError('remote_model_port_not_exclusively_loopback', 4)
        health = value['services'].get(str(port), {})
        if not isinstance(health, dict) or health.get('ready') is not True or health.get('failed') is True:
            raise TunnelError('remote_model_service_not_ready', 5)
    return value


def remote_attestation(control):
    response = run(mux_args(control) + ['-o', 'ClearAllForwardings=yes', TARGET, '/usr/bin/python3', '-'], data=REMOTE, timeout=40)
    if response.returncode or len(response.stdout) > 16384:
        raise TunnelError('authenticated_remote_attestation_unavailable', 4)
    try:
        value = json.loads(response.stdout)
    except ValueError as exc:
        raise TunnelError('invalid_remote_attestation', 4) from exc
    return validate_remote(value)


def listeners(port):
    result = run([lsof_path(), '-nP', '-iTCP:' + str(port), '-sTCP:LISTEN', '-Fpn'])
    if result.returncode not in {0, 1}:
        raise TunnelError('local_listener_inventory_failed', 5)
    records, pid = [], None
    for line in result.stdout.splitlines():
        if line.startswith('p'):pid = int(line[1:])
        elif line.startswith('n'):records.append({'pid': pid, 'address': line[1:]})
    return records


def local_health(port):
    path = '/healthz' if port == 8002 else '/health'
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:' + str(port) + path, timeout=3) as response:
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                return False
            value = json.loads(raw)
        return value.get('ready') is True and value.get('failed') is not True
    except Exception:
        return False


def local_tcp_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.3):
            return True
    except OSError:
        return False


def diagnose(control=CONTROL, recover=False):
    control = Path(control)
    result = dict(ready=False, status='unverified', target=TARGET, ssh_port=SSH_PORT,
        allowed_loopback_ports=list(PORTS), existing_authenticated_master_required=True,
        new_login_attempted=False, recovery_performed=False, models_or_services_restarted=False,
        remote_model_and_research_state='unverified', local_tcp_open={str(port): local_tcp_open(port) for port in PORTS})
    try:
        if not control.is_absolute():
            raise TunnelError('absolute_control_socket_path_required', 2)
        master_pid = verified_master(control)
        remote = remote_attestation(control)
        result['remote'] = remote
        result['remote_model_and_research_state'] = 'model_APIs_attested_research_not_probed'
        missing = []
        for port in PORTS:
            rows = listeners(port)
            if any(row != {'pid': master_pid, 'address': '127.0.0.1:' + str(port)} for row in rows):
                raise TunnelError('local_port_has_foreign_owner_or_non_loopback_binding', 5)
            if not rows:
                missing.append(port)
        if recover and missing:
            # No server-side command/start/restart: only an existing master's two
            # fixed forwards. A failed multi-forward request may be partial; we
            # never cancel or signal an existing operator's connections.
            argv = mux_args(control) + ['-o', 'ClearAllForwardings=no', '-o', 'ExitOnForwardFailure=yes', '-O', 'forward']
            for port in missing:
                argv += ['-L', '127.0.0.1:' + str(port) + ':127.0.0.1:' + str(port)]
            response = run(argv + [TARGET])
            if response.returncode:
                result['recovery_may_be_partial'] = True
                raise TunnelError('forward_request_failed_recheck_status', 5)
            result.update(recovery_performed=True, added_loopback_ports=missing)
        checks = {}
        for port in PORTS:
            rows = listeners(port)
            owned = bool(rows) and all(row == {'pid': master_pid, 'address': '127.0.0.1:' + str(port)} for row in rows)
            checks[str(port)] = dict(private_master_listener=owned, api_ready=local_health(port) if owned else False)
        result['local_ports'] = checks
        result['ready'] = all(value['private_master_listener'] and value['api_ready'] for value in checks.values())
        result['status'] = 'ready' if result['ready'] else 'forward_or_local_API_not_ready'
        return result, 0 if result['ready'] else 5
    except TunnelError as exc:
        result.update(status='authentication_unavailable' if exc.code == 3 else 'verification_failed', reason=exc.reason)
        return result, exc.code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'recover'), nargs='?', default='status')
    parser.add_argument('--control-path', type=Path, default=CONTROL)
    args = parser.parse_args(argv)
    result, code = diagnose(args.control_path, args.action == 'recover')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
