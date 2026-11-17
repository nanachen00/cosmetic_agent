#!/usr/bin/env python3
"""Control only secure, loopback BGE instances: formal8002 and candidate8027.

First migration refuses an occupied legacy8002. Only the primary operator may
retire that process after a fresh identity/idle review; this tool never adopts it.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path('/mnt/goodagents')
EXPECTED_NFS = '192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
SOURCE = ROOT / 'blink-runtime/src/bge-secure-serving-20261116-v2'
PYTHON = ROOT / 'blink-runtime/envs/bge-secure-t213-r71/bin/python'
ACTIVE = ROOT / 'outputs/runtime/bge-model.active.json'
STATE_DIR = ROOT / 'outputs/runtime/pids'
INSTANCES = {'bge-model': 8002, 'bge-model-candidate': 8027}
REVISION = '5617a9f61b028005a4858fdac845db406aefb181'
NATIVE = {'torch': '2.13.0+rocm7.1', 'torchvision': '0.28.0+rocm7.1', 'triton-rocm': '3.7.1'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_mount():
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', str(ROOT)], text=True))['filesystems'][0]
    if (mount.get('target') != str(ROOT) or mount.get('fstype') not in {'nfs', 'nfs4'}
            or mount.get('source') != EXPECTED_NFS or 'rw' not in mount.get('options', '').split(',')):
        raise SystemExit('Expected writable school NFS mount missing; refusing mkdir/start/signal')
    return mount


def identity(record, instance):
    try:
        source, py = Path(record['source']), Path(record['python'])
        return (record['service'] == 'blink-managed-bge-v1' and record['instance'] == instance
            and type(record['pid']) is int and record['pid'] > 1
            and record['port'] == INSTANCES[instance] and source.is_absolute() and py.is_absolute()
            and source.resolve().is_relative_to(ROOT) and py.resolve().is_relative_to(ROOT)
            and record['command'] == [str(py), '-u', str(source / 'bge_secure_service.py'), '--port', str(INSTANCES[instance])])
    except (KeyError, TypeError, ValueError, OSError):
        return False


def live(record):
    try:
        process = Path('/proc') / str(int(record['pid']))
        return (process.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19] == str(record['start_ticks'])
            and process.joinpath('exe').resolve() == Path(record['python']).resolve()
            and process.joinpath('cmdline').read_bytes().rstrip(b'\0').split(b'\0') == [value.encode() for value in record['command']])
    except (KeyError, TypeError, ValueError, OSError, IndexError):
        return False


def listeners(port):
    rows = []
    for line in subprocess.check_output(['ss', '-H', '-ltnp', f'sport = :{port}'], text=True).splitlines():
        fields = line.split()
        if len(fields) < 4:
            raise SystemExit('Unrecognized listener inventory; refusing ownership assumptions')
        rows.append({'address': fields[3], 'pids': sorted({int(value) for value in re.findall(r'pid=(\d+)', line)})})
    return rows


def owns_loopback(record, rows):
    return rows == [{'address': '127.0.0.1:' + str(record['port']), 'pids': [record['pid']]}]


def health(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/healthz', timeout=2) as response:
            raw = response.read(4097)
        return json.loads(raw) if len(raw) <= 4096 else None
    except (OSError, ValueError, urllib.error.HTTPError):
        return None


def is_ready(payload):
    return (isinstance(payload, dict) and payload.get('ready') is True and payload.get('status') == 'ready'
        and payload.get('revision') == REVISION and payload.get('dimension') == 1024 and payload.get('model') == 'BAAI/bge-m3')


def wait_ready(record, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not live(record):
            raise SystemExit('BGE exited or changed identity')
        rows = listeners(record['port'])
        if rows and not owns_loopback(record, rows):
            raise SystemExit('BGE port ownership/bind differs; refusing readiness')
        payload = health(record['port']) if rows else None
        if is_ready(payload):
            return payload
        time.sleep(1)
    raise SystemExit('BGE readiness timeout')


def clean_env():
    env = dict(os.environ, HF_HOME=str(ROOT / 'hf-cache'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
        PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4', TOKENIZERS_PARALLELISM='false')
    for name in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH'):
        env.pop(name, None)
    return env


def verified_runtime(source, py):
    if not source.is_absolute() or not py.is_absolute() or not source.resolve().is_relative_to(ROOT) or not py.resolve().is_relative_to(ROOT):
        raise SystemExit('BGE source and Python must be persistent absolute paths')
    required = {'bge_secure_service.py', 'bounded_http.py', 'bge-m3.secure.manifest.json', 'requirements-bge-linux-py311.lock'}
    manifest_file = source / 'source-manifest.json'
    expected = json.loads(manifest_file.read_text())
    if not required.issubset(expected):
        raise SystemExit('Incomplete BGE serving source manifest')
    if {str(path.relative_to(source)) for path in source.rglob('*')} != set(expected) | {'source-manifest.json'}:
        raise SystemExit('Unreviewed files in BGE immutable serving source')
    for name, checksum in expected.items():
        path = source / name
        if not path.resolve().is_relative_to(source) or digest(path) != checksum:
            raise SystemExit('BGE serving source checksum differs')
    metadata = json.loads((py.parent.parent / 'runtime-ready.json').read_text())
    lock = source / 'requirements-bge-linux-py311.lock'
    if metadata.get('profile') != 'secure-rocm71' or metadata.get('python_lock_sha256') != digest(lock):
        raise SystemExit('The reviewed secure BGE profile/hash lock is required')
    versions = dict(NATIVE)
    for line in lock.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            name, version = line.split()[0].split('==')
            versions[name] = version
    env = clean_env()
    code = 'import importlib.metadata as m,json;print(json.dumps({name:m.version(name) for name in ' + repr(list(versions)) + '}))'
    installed = json.loads(subprocess.check_output([str(py), '-c', code], env=env, text=True, timeout=30))
    if installed != versions:
        raise SystemExit('Installed BGE packages differ from the reviewed lock')
    library = subprocess.check_output([str(py), '-c', 'import pathlib,importlib.util;print(pathlib.Path(importlib.util.find_spec("torch").origin).parent/"lib")'], env=env, text=True, timeout=30).strip()
    env['LD_LIBRARY_PATH'] = library + ':/opt/rocm/lib:/opt/rocm/lib64'
    return metadata, env, digest(manifest_file)


def active_config():
    value = json.loads(ACTIVE.read_text())
    if (value.get('instance') != 'bge-model' or value.get('port') != 8002 or value.get('profile') != 'secure-rocm71'):
        raise SystemExit('Invalid secure BGE active pointer')
    source, py = Path(value['source']), Path(value['python'])
    metadata, _, manifest_sha = verified_runtime(source, py)
    if (value.get('source_manifest_sha256') != manifest_sha or value.get('python_lock_sha256') != metadata['python_lock_sha256']
            or value.get('native_lock_sha256') != metadata['native_lock_sha256']):
        raise SystemExit('BGE active source/environment checksum differs')
    return value


def activate(record, metadata, manifest_sha):
    require_mount()
    value = {key: record[key] for key in ('instance', 'port', 'source', 'python')}
    value.update(profile='secure-rocm71', source_manifest_sha256=manifest_sha,
        python_lock_sha256=metadata['python_lock_sha256'], native_lock_sha256=metadata['native_lock_sha256'],
        activated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    partial = ACTIVE.with_suffix('.partial')
    partial.write_text(json.dumps(value, indent=2) + '\n')
    partial.chmod(0o600)
    partial.replace(ACTIVE)


def stop(record, state):
    if not live(record):
        raise SystemExit('BGE PID identity differs; refusing to signal')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise SystemExit('pidfd support required; no numeric PID signal fallback')
    descriptor = os.pidfd_open(record['pid'], 0)
    try:
        if not live(record):
            raise SystemExit('BGE PID identity changed after pidfd acquisition')
        require_mount()
        rows = listeners(record['port'])
        if rows and not owns_loopback(record, rows):
            raise SystemExit('BGE listener owner/bind changed; refusing to signal')
        payload = health(record['port']) if rows else None
        if rows and (not is_ready(payload) or payload.get('busy') is not False):
            raise SystemExit('Cannot confirm BGE idle; retry after inference/recovery')
        signal.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not live(record) and not listeners(record['port']):
            state.unlink()
            print('BGE_STOPPED')
            return
        time.sleep(.1)
    raise SystemExit('BGE did not stop or port remains occupied; no forced kill sent')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'stop'])
    parser.add_argument('--instance', choices=INSTANCES, default='bge-model-candidate')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--wait', type=int, default=180)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args(argv)
    if args.activate and (args.action != 'start' or args.instance != 'bge-model'):
        raise SystemExit('Activation is only valid for formal BGE start')
    if not 1 <= args.wait <= 1800:
        raise SystemExit('Invalid readiness wait')
    require_mount()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = STATE_DIR / (args.instance + '.identity.json')
    with state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(state.read_text()) if state.exists() else None
        if record and not identity(record, args.instance):
            raise SystemExit('Foreign BGE identity; refusing to act')
        if args.action == 'status':
            running = bool(record and live(record))
            if running:
                verified_runtime(Path(record['source']), Path(record['python']))
            ready = bool(running and owns_loopback(record, listeners(record['port'])) and is_ready(health(record['port'])))
            print(json.dumps({'running': running, 'ready': ready, 'identity': record}))
            return 0 if ready else 1
        if args.action == 'stop':
            if record:
                stop(record, state)
            return 0
        explicit_activation = args.activate and args.source is not None and args.python is not None
        active = active_config() if args.instance == 'bge-model' and ACTIVE.exists() and not explicit_activation else None
        if args.instance == 'bge-model' and not active and not args.activate:
            raise SystemExit('Formal BGE requires a reviewed --activate migration or its persistent secure active pointer')
        source = (args.source or Path(active['source'] if active else SOURCE)).absolute().resolve()
        py = (args.python or Path(active['python'] if active else PYTHON)).absolute()
        if active and not args.activate and (str(source) != active['source'] or str(py) != active['python']):
            raise SystemExit('Formal BGE override differs from its active pointer; explicit reviewed activation required')
        metadata, env, manifest_sha = verified_runtime(source, py)
        if record and live(record):
            if record['source'] != str(source) or record['python'] != str(py):
                raise SystemExit('Existing BGE configuration differs; refusing takeover')
            print(json.dumps(wait_ready(record, args.wait)))
            if args.activate:
                activate(record, metadata, manifest_sha)
            return 0
        port = INSTANCES[args.instance]
        if listeners(port):
            raise SystemExit('BGE port occupied; refusing legacy/unknown process takeover')
        subprocess.run([str(py), '-c', 'import torch;assert torch.__version__=="2.13.0+rocm7.1" and torch.version.hip and torch.cuda.is_available() and "MI300X" in torch.cuda.get_device_name(0)'], env=env, check=True, timeout=180)
        log = ROOT / 'outputs/runtime/logs' / (args.instance + '.log')
        require_mount()
        log.parent.mkdir(parents=True, exist_ok=True)
        command = [str(py), '-u', str(source / 'bge_secure_service.py'), '--port', str(port)]
        require_mount()
        with log.open('ab', buffering=0) as output:
            process = subprocess.Popen(command, env=env, cwd=source, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        ticks = (Path('/proc') / str(process.pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        record = dict(service='blink-managed-bge-v1', instance=args.instance, pid=process.pid, start_ticks=ticks,
            port=port, source=str(source), python=str(py), command=command, log=str(log))
        partial = state.with_suffix('.partial')
        partial.write_text(json.dumps(record, indent=2) + '\n')
        partial.chmod(0o600)
        partial.replace(state)
        print('BGE_STARTING', args.instance, process.pid, flush=True)
        print(json.dumps(wait_ready(record, args.wait)), flush=True)
        if args.activate:
            activate(record, metadata, manifest_sha)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
