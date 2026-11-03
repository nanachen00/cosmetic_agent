#!/usr/bin/env python3
"""Read-only, bounded diagnosis of the current :8010 preflight prerequisite.

Writes only a new diagnostics directory/archive under
<root>/ops/local-llm-path-benchmark/diagnostics/.

Does not import the application, read process environments/cookies, install
packages, change source/configuration, or start/stop/restart a service. Optional
HTTP probes are GET requests to literal 127.0.0.1:8010; redirects/proxies are
not followed. Access logs and existing health-check dependencies can be touched
by those GETs. Use --skip-http to omit them.

Log/source redaction is best effort, NOT a complete personal-data/secret filter.
Review the new output before sharing. No original files are deleted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

DEFAULT_ROOT = Path('/var/lib/jenkins/tomod')
PORTS = {80, 6379, 6380, 8000, 8001, 8002, 8003, 8010, 8011, 8200, 8203, 8206, 8210}
TEXT_LIMIT = 131072


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def scrub(value: str) -> str:
    value = re.sub(r'-----BEGIN [^\n]*PRIVATE KEY-----.*?-----END [^\n]*PRIVATE KEY-----',
                   '<REDACTED_PRIVATE_KEY>', value, flags=re.S)
    value = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@',
                   r'\1<REDACTED_USERINFO>@', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '<REDACTED_API_KEY>', value)
    value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer <REDACTED>', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b',
                   '<REDACTED_JWT>', value)
    value = re.sub(r'(?im)(\b(?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*',
                   r'\1<REDACTED>', value)
    value = re.sub(
        r'''(?i)(["']?(?:[A-Za-z0-9_]*(?:api_key|access_token|refresh_token|secret|password|passwd))["']?\s*[:=]\s*)(["'])([^"'\r\n]*)(\2)''',
        r'\1"<REDACTED>"', value)
    value = re.sub(
        r'(?im)^(\s*(?:export\s+)?[A-Za-z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|SECRET|PASSWORD|PASSWD)\s*=)\s*[^\r\n]+',
        r'\1<REDACTED>', value)
    return value


def safe_argv(args: list[str]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            result.append('<REDACTED>')
            redact_next = False
            continue
        sensitive = bool(re.search(r'(?i)(api[-_]key|password|passwd|secret|token|credential)', arg.split('=', 1)[0]))
        if arg.startswith('-') and sensitive:
            if '=' in arg:
                result.append(arg.split('=', 1)[0] + '=<REDACTED>')
            else:
                result.append(arg)
                redact_next = True
        else:
            result.append(scrub(arg))
    return result


def error_record(exc: BaseException) -> dict[str, Any]:
    number = getattr(exc, 'errno', None)
    return {'type': type(exc).__name__, 'errno': number,
            'errno_name': errno.errorcode.get(number), 'message': scrub(str(exc))[:1200]}


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)


def command(args: list[str], timeout: float = 3.0) -> dict[str, Any]:
    started = now()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors='replace',
                              timeout=timeout, check=False, env={**os.environ, 'LC_ALL': 'C'})
        return {'started_at': started, 'ended_at': now(), 'argv': safe_argv(args),
                'status': 'completed', 'returncode': proc.returncode,
                'stdout': scrub(proc.stdout)[:TEXT_LIMIT], 'stderr': scrub(proc.stderr)[:TEXT_LIMIT],
                'output_truncated': len(proc.stdout) > TEXT_LIMIT or len(proc.stderr) > TEXT_LIMIT}
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Do NOT translate an unsuccessful diagnostic into "port absent".
        return {'started_at': started, 'ended_at': now(), 'argv': safe_argv(args),
                'status': 'unknown', 'error': error_record(exc)}


def ss_probe(with_owners: bool) -> dict[str, Any]:
    executable = shutil.which('ss')
    if not executable:
        return {'status': 'unknown', 'reason': 'ss not found', 'timestamp': now()}
    return command([executable, '-H', '-ltnp' if with_owners else '-ltn', 'sport = :8010'])


def related_ports() -> dict[str, Any]:
    executable = shutil.which('ss')
    if not executable:
        return {'status': 'unknown', 'reason': 'ss not found'}
    result = command([executable, '-H', '-ltn'])
    if result.get('status') == 'completed':
        selected = []
        for line in result['stdout'].splitlines():
            fields = line.split()
            if len(fields) > 3:
                try:
                    port = int(fields[3].rsplit(':', 1)[1])
                except (ValueError, IndexError):
                    continue
                if port in PORTS:
                    selected.append(line)
        result['stdout'] = '\n'.join(selected)
        result['selection'] = sorted(PORTS)
    return result


def proc_tcp_tables() -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for name in ('tcp', 'tcp6'):
        path = Path('/proc/net') / name
        try:
            rows = []
            for line in path.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    continue
                if int(fields[1].rsplit(':', 1)[1], 16) != 8010:
                    continue
                rows.append({'local_address_hex': fields[1], 'state_hex': fields[3],
                             'listen': fields[3] == '0A', 'uid': fields[7], 'socket_inode': fields[9]})
            tables[str(path)] = {'status': 'read', 'timestamp': now(), 'rows': rows}
        except (OSError, ValueError) as exc:
            tables[str(path)] = {'status': 'unknown', 'error': error_record(exc)}
    return tables


def readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def cmdline(pid: int) -> list[str]:
    with (Path('/proc') / str(pid) / 'cmdline').open('rb') as stream:
        raw = stream.read(32768)
    return [s.decode(errors='replace') for s in raw.split(b'\0') if s]


def start_ticks(pid: int) -> str | None:
    try:
        text = (Path('/proc') / str(pid) / 'stat').read_text()
        return text[text.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def process_info(pid: int) -> dict[str, Any]:
    proc = Path('/proc') / str(pid)
    first = start_ticks(pid)
    obj: dict[str, Any] = {'pid': pid, 'observed_at': now(), 'start_ticks_before': first}
    try:
        args = cmdline(pid)
        obj['argv'] = safe_argv(args)
        obj['matches_backend_app'] = 'api.api_server:app' in args
        obj['status_excerpt'] = [s for s in (proc / 'status').read_text().splitlines()
                                 if s.startswith(('Name:', 'State:', 'Pid:', 'PPid:', 'Uid:', 'Threads:', 'VmRSS:'))]
    except OSError as exc:
        obj['read_error'] = error_record(exc)
    for name in ('exe', 'cwd', 'fd/1', 'fd/2', 'ns/net', 'ns/pid', 'ns/mnt'):
        obj[name] = readlink(proc / name)
    obj['start_ticks_after'] = start_ticks(pid)
    obj['identity_stable_during_read'] = first is not None and first == obj['start_ticks_after']
    return obj


def discover_backend_processes(seed: set[int], deadline_seconds: float = 3.0) -> dict[str, Any]:
    started = time.monotonic()
    found = set(seed)
    scanned = unreadable = 0
    complete = True
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():
            continue
        if time.monotonic() - started > deadline_seconds:
            complete = False
            break
        scanned += 1
        try:
            args = cmdline(int(item.name))
            if 'api.api_server:app' in args:
                found.add(int(item.name))
        except OSError:
            unreadable += 1
    return {'scope': 'visible /proc only; exact api.api_server:app argv match plus observed owner/pidfile PIDs',
            'scan_complete': complete, 'unreadable_processes': unreadable, 'scanned_processes': scanned,
            'processes': [process_info(pid) for pid in sorted(found)]}


def regular_file_metadata(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        return {'path': str(path), 'exists': True, 'is_regular': stat.S_ISREG(st.st_mode),
                'size': st.st_size, 'mtime_utc': dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat(),
                'inode': st.st_ino, 'symlink': path.is_symlink()}
    except OSError as exc:
        return {'path': str(path), 'exists': None if not isinstance(exc, FileNotFoundError) else False,
                'error': error_record(exc)}


def pidfile_info(root: Path) -> dict[str, Any]:
    path = root / 'ops/runtime/pids/backend-8010.pid'
    obj = regular_file_metadata(path)
    if obj.get('is_regular') and obj.get('size', 9999) < 128:
        try:
            text = path.read_text().strip()
            obj['pid'] = int(text) if text.isdigit() and int(text) > 0 else None
            obj['parse_ok'] = obj['pid'] is not None
        except (OSError, ValueError) as exc:
            obj['error'] = error_record(exc)
    obj['note'] = 'A PID file is a hint, not proof that its PID owns a listener.'
    return obj


def tcp_probe() -> dict[str, Any]:
    start = time.monotonic()
    result: dict[str, Any] = {'started_at': now(), 'target': '127.0.0.1:8010'}
    try:
        with socket.create_connection(('127.0.0.1', 8010), timeout=1.5):
            result['connected'] = True
    except OSError as exc:
        result['connected'] = False
        result['error'] = error_record(exc)
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000, 3)
    result['ended_at'] = now()
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_probe(path: str) -> dict[str, Any]:
    if path not in ('/api/v1/live', '/api/v1/health'):
        raise ValueError('Unexpected diagnostic endpoint')
    url = 'http://127.0.0.1:8010' + path
    request_id = 'diag-' + uuid.uuid4().hex
    result: dict[str, Any] = {'started_at': now(), 'url': url, 'request_id': request_id,
                             'proxies': False, 'redirects_followed': False}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'BLINK-8010-Diagnostic',
                                                   'X-Request-ID': request_id}, method='GET')
    start = time.monotonic()
    try:
        try:
            response = opener.open(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result['status'] = response.code
            result['headers'] = {k: scrub(v) for k, v in response.headers.items()
                                 if k.lower() in {'date', 'server', 'content-type', 'content-length', 'x-request-id'}}
            body = response.read(4097)
            result['body_preview'] = scrub(body[:4096].decode(errors='replace'))
            result['body_truncated'] = len(body) > 4096
    except (OSError, urllib.error.URLError) as exc:
        result['status'] = None
        result['error'] = error_record(exc)
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000, 3)
    result['ended_at'] = now()
    return result


def snapshot_file(src: Path, dest: Path, max_bytes: int, tail: bool) -> dict[str, Any]:
    obj = regular_file_metadata(src)
    if not obj.get('is_regular'):
        return obj
    if obj.get('symlink'):
        obj['copy_skipped'] = 'symlink; only regular explicit source files are copied'
        return obj
    try:
        with src.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if tail:
                stream.seek(max(0, size - max_bytes))
            data = stream.read(max_bytes)
        clean = scrub(data.decode(errors='replace'))
        dest.write_text(clean, encoding='utf-8')
        dest.chmod(0o600)
        obj.update({'copied_bytes_before_redaction': len(data), 'excerpt_file': dest.name,
                    'is_tail': tail, 'truncated': size > max_bytes, 'redaction': 'best-effort'})
    except OSError as exc:
        obj['error'] = error_record(exc)
    return obj


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--skip-http', action='store_true')
    parser.add_argument('--log-tail-kib', type=int, choices=range(0, 257), default=128, metavar='0..256')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Linux server required; run this on the server used for the failed preflight.')
    root = Path(args.root).resolve(strict=True)
    if not root.is_dir():
        parser.error('--root must be an existing project directory')
    os.umask(0o077)
    name = 'service-8010-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    parent = root / 'ops/local-llm-path-benchmark/diagnostics'
    parent.mkdir(parents=True, exist_ok=True)
    out = parent / name
    out.mkdir(mode=0o700, exist_ok=False)
    print('[OUTPUT]', out, flush=True)
    identity = {'started_at': now(), 'python': sys.executable, 'python_version': sys.version,
                'hostname': socket.gethostname(), 'uid': os.getuid(), 'euid': os.geteuid(),
                'root': str(root), 'cwd': str(Path.cwd()),
                'collector_namespaces': {k: readlink(Path('/proc/self/ns') / k) for k in ('net', 'pid', 'mnt')},
                'scope': 'Point-in-time observations in this process/container; not historical proof or service recovery.',
                'service_lifecycle_actions': [], 'application_imports': False, 'process_environment_read': False}
    write_json(out / 'collector.json', identity)
    print('[CHECK] listeners and independent /proc socket tables', flush=True)
    before = {'timestamp': now(), 'ss_no_owner': ss_probe(False), 'ss_with_owner': ss_probe(True),
              'proc_tcp': proc_tcp_tables()}
    write_json(out / 'listeners-before.json', before)
    write_json(out / 'related-listeners.json', related_ports())
    pidfile = pidfile_info(root)
    write_json(out / 'pidfile.json', pidfile)
    seeds = set(map(int, re.findall(r'pid=(\d+)', before['ss_with_owner'].get('stdout', ''))))
    if pidfile.get('pid'):
        seeds.add(pidfile['pid'])
    write_json(out / 'backend-processes.json', discover_backend_processes(seeds))
    print('[CHECK] TCP connect' + ('; HTTP skipped' if args.skip_http else '; loopback GET live/health'), flush=True)
    tcp_result = tcp_probe()
    http_results = [] if args.skip_http else [http_probe(p) for p in ('/api/v1/live', '/api/v1/health')]
    write_json(out / 'tcp-8010.json', tcp_result)
    write_json(out / 'http-8010.json', {'skipped': args.skip_http, 'probes': http_results})
    print('[OBSERVED] TCP connected=' + str(tcp_result.get('connected')), flush=True)
    for result in http_results:
        print('[OBSERVED]', result['url'], 'HTTP status=' + str(result.get('status')), flush=True)
    print('[CHECK] bounded log/lifecycle excerpts and harness hashes', flush=True)
    file_records = []
    if args.log_tail_kib:
        file_records.append(snapshot_file(root / 'ops/runtime/logs/backend-8010.log',
                            out / 'backend-8010-tail.log', args.log_tail_kib * 1024, True))
    for base in ('start-production.sh', 'stop-production.sh', 'status-production.sh'):
        file_records.append(snapshot_file(root / 'ops/runtime' / base,
                            out / (base + '.review.txt'), TEXT_LIMIT, False))
    # Hash only. Never import the backend/harness or change its manifest.
    hashes = []
    for relative in ('ops/local-llm-path-benchmark/bench.py',
                     'ops/local-llm-path-benchmark/process_control.py',
                     'ops/local-llm-path-benchmark/MANIFEST.sha256',
                     'ops/local-llm-path-benchmark/config.json',
                     'cosmetic-agent/api/api_server.py'):
        path = root / relative
        record = regular_file_metadata(path)
        if record.get('is_regular') and record.get('size', 0) <= 4 * 1024 * 1024:
            try:
                record['sha256_at_read'] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                record['error'] = error_record(exc)
        hashes.append(record)
    write_json(out / 'file-provenance.json', {'excerpts': file_records, 'hashes_only': hashes})
    write_json(out / 'listeners-after.json', {'timestamp': now(), 'ss_no_owner': ss_probe(False),
                                            'proc_tcp': proc_tcp_tables()})
    (out / 'README.txt').write_text(
        'Read-only service prerequisite diagnosis. No service recovery was attempted.\n'
        'Each observation has its own timestamp; a restart during collection can produce different results.\n'
        'Command timeout/error means UNKNOWN, not an absent listener.\n'
        'Only selected service log tails and lifecycle script excerpts are copied. No .env, cookies or /proc/PID/environ.\n'
        'Log/source redaction is best effort. Check for personal data and credentials before uploading.\n'
        'Exit code 0 means collection completed, NOT that :8010 is healthy.\n', encoding='utf-8')
    manifest = []
    for p in sorted(out.iterdir()):
        if p.is_file():
            manifest.append(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}')
    (out / 'SHA256SUMS').write_text('\n'.join(manifest) + '\n', encoding='utf-8')
    archive = parent / (name + '.tar.gz')
    with archive.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as tf:
        tf.add(out, arcname=name, recursive=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print('[COLLECTION_COMPLETE] This is not a health PASS.', flush=True)
    print('[ARCHIVE]', archive, flush=True)
    print('[SHA256]', digest, flush=True)
    print('Review log/source excerpts before sharing. No original file was removed.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print('[COLLECTION_FAILED]', scrub(str(exc)), file=sys.stderr)
        raise SystemExit(1)
