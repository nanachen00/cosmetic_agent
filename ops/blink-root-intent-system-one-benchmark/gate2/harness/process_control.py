"""Bounded process control: production processes are capture/verify-only.
Only children created and journaled by this benchmark may receive SIGTERM.
Shared :8200 is protected, even if its settings/PID change during deployment. No pkill, killall, SIGKILL, process-group or global signals.
"""
from __future__ import annotations
import os
import ctypes
import errno
import sys
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import time
# Support the diagnostic entry point under Python -I without importing the app.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Refuse, atomic_json, read_json, sha_bytes

PROTECTED = {80, 6379, 8000, 8001, 8002, 8003, 8010, 8011, 8200}

_LIBC = None

# Verified, deliberately narrow ABI fallback. These numbers are NOT portable
# across arbitrary Linux ABIs. We only use them on native x86_64 LP64 below.
# Source: Linux v6.12, arch/x86/entry/syscalls/syscall_64.tbl
# https://github.com/torvalds/linux/blob/v6.12/arch/x86/entry/syscalls/syscall_64.tbl
_X86_64_PIDFD_SYSCALLS = {'pidfd_open': 434, 'pidfd_send_signal': 424}


def _libc():
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def _python_binding(name: str):
    """Return a callable or None; separate for explicit missing-wrapper tests."""
    module = os if name == 'pidfd_open' else signal
    fn = getattr(module, name, None)
    return fn if callable(fn) else None


def _libc_binding(name: str):
    fn = getattr(_libc(), name, None)
    return fn if callable(fn) else None


def _pidfd_runtime() -> dict:
    """Read only the invoking interpreter's ABI, not a service environment."""
    machine = os.uname().machine if hasattr(os, 'uname') else ''
    release = os.uname().release if hasattr(os, 'uname') else ''
    try:
        libc_version = os.confstr('CS_GNU_LIBC_VERSION')
    except (AttributeError, OSError, ValueError):
        libc_version = None
    return {
        'python_executable': sys.executable,
        'python_version': sys.version.split()[0],
        'sys_platform': sys.platform,
        'machine': machine,
        'kernel_release': release,
        'libc_version': libc_version,
        'pointer_bits': ctypes.sizeof(ctypes.c_void_p) * 8,
        'long_bits': ctypes.sizeof(ctypes.c_long) * 8,
        'int_bits': ctypes.sizeof(ctypes.c_int) * 8,
        'byteorder': sys.byteorder,
    }


def _syscall_abi_ok(runtime: dict) -> bool:
    # In particular, reject 32-bit compatibility / x32 even on an x86_64 kernel.
    return (
        runtime.get('sys_platform') == 'linux'
        and runtime.get('machine') == 'x86_64'
        and runtime.get('pointer_bits') == 64
        and runtime.get('long_bits') == 64
        and runtime.get('int_bits') == 32
        and runtime.get('byteorder') == 'little'
    )


def _binding_for(name: str) -> str | None:
    if name not in _X86_64_PIDFD_SYSCALLS:
        raise Refuse('Unreviewed pidfd operation')
    if _python_binding(name) is not None:
        module = 'os' if name == 'pidfd_open' else 'signal'
        return f'python:{module}.{name}'
    if _libc_binding(name) is not None:
        return f'libc:{name}'
    if _syscall_abi_ok(_pidfd_runtime()) and _libc_binding('syscall') is not None:
        number = _X86_64_PIDFD_SYSCALLS[name]
        return f'libc:syscall(SYS_{name}={number})'
    return None


def _pidfd_syscall(name: str, *args) -> int:
    """Invoke a reviewed pidfd syscall, without bypassing runtime denials.

    Only selected when BOTH the Python and named libc wrapper are missing.
    ENOSYS/EPERM/EACCES from an existing wrapper are never used as a reason to
    retry through a different path. No security policy or kernel setting changes.
    """
    if name not in _X86_64_PIDFD_SYSCALLS or not _syscall_abi_ok(_pidfd_runtime()):
        raise Refuse('Direct pidfd syscall unsupported for this process ABI')
    fn = _libc_binding('syscall')
    if fn is None:
        raise Refuse('libc syscall entry unavailable')
    # syscall(long number, ...): type the fixed argument, explicitly type each
    # variadic argument at the call site; preserve the C long return width.
    fn.argtypes = [ctypes.c_long]
    fn.restype = ctypes.c_long
    ctypes.set_errno(0)
    rc = fn(ctypes.c_long(_X86_64_PIDFD_SYSCALLS[name]), *args)
    if rc == -1:
        err = ctypes.get_errno() or errno.EIO
        raise OSError(err, os.strerror(err))
    return int(rc)


def _positive_pid(pid: int) -> int:
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 2147483647:
        raise ValueError('pid must be a positive 32-bit process ID')
    return pid


def _pidfd_open(pid: int) -> int:
    """Open a process-bound descriptor, never a numeric-PID signal fallback."""
    pid = _positive_pid(pid)
    binding = _binding_for('pidfd_open')
    if binding is None:
        raise Refuse('pidfd_open binding unavailable for this process ABI')
    if binding.startswith('python:'):
        fd = _python_binding('pidfd_open')(pid, 0)
    elif binding == 'libc:pidfd_open':
        fn = _libc_binding('pidfd_open')
        fn.argtypes = [ctypes.c_int, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        fd = fn(pid, 0)
        if fd < 0:
            err = ctypes.get_errno() or errno.EIO
            raise OSError(err, os.strerror(err))
    else:
        fd = _pidfd_syscall('pidfd_open', ctypes.c_int(pid), ctypes.c_uint(0))
    # pidfd_open sets CLOEXEC; enforce it through Python as well.
    try:
        os.set_inheritable(fd, False)
    except BaseException:
        os.close(fd)
        raise
    return int(fd)


def _pidfd_send_signal(fd: int, sig: int) -> None:
    """Use the pidfd opened for the verified process. Missing wrappers only
    select another binding; runtime errors propagate without fallback."""
    if not isinstance(fd, int) or isinstance(fd, bool) or not 0 <= fd <= 2147483647:
        raise ValueError('fd must be a nonnegative 32-bit file descriptor')
    if not isinstance(sig, int) or isinstance(sig, bool) or not 0 <= sig < signal.NSIG:
        raise ValueError('signal must be 0 or a valid signal number')
    binding = _binding_for('pidfd_send_signal')
    if binding is None:
        raise Refuse('pidfd_send_signal binding unavailable for this process ABI')
    if binding.startswith('python:'):
        _python_binding('pidfd_send_signal')(fd, sig)
    elif binding == 'libc:pidfd_send_signal':
        fn = _libc_binding('pidfd_send_signal')
        fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = fn(fd, sig, None, 0)
        if rc != 0:
            err = ctypes.get_errno() or errno.EIO
            raise OSError(err, os.strerror(err))
    else:
        rc = _pidfd_syscall(
            'pidfd_send_signal', ctypes.c_int(fd), ctypes.c_int(sig),
            ctypes.c_void_p(None), ctypes.c_uint(0),
        )
        if rc != 0:
            raise Refuse('Unexpected pidfd_send_signal syscall result')


def pidfd_capability() -> dict:
    """Self-only signal-0 + pollability probe. No service lookup/stop/restart.

    usable=True establishes this caller's tested primitive, not authority to
    signal a different process. terminate() still enforces protected ports,
    ownership and process identity, and propagates any per-target denial.
    """
    result = {
        'usable': False,
        'pidfd_open_binding': None,
        'pidfd_send_signal_binding': None,
        'runtime_probe': 'not-run',
        'probe_stage': 'resolve-bindings',
        'kernel_probe_attempted': False,
        'probe_target': 'self',
        'probe_signal': 0,
        'runtime': _pidfd_runtime(),
        'availability': {},
        'error_errno': None,
        'error_name': None,
        'error_category': None,
        'fallback_on_runtime_error': False,
    }
    fd = None
    try:
        abi_ok = _syscall_abi_ok(result['runtime'])
        result['availability'] = {
            'python_pidfd_open': _python_binding('pidfd_open') is not None,
            'python_pidfd_send_signal': _python_binding('pidfd_send_signal') is not None,
            'libc_pidfd_open': _libc_binding('pidfd_open') is not None,
            'libc_pidfd_send_signal': _libc_binding('pidfd_send_signal') is not None,
            'libc_syscall': _libc_binding('syscall') is not None,
            'reviewed_syscall_abi': abi_ok,
        }
        for name in ('pidfd_open', 'pidfd_send_signal'):
            result[name + '_binding'] = _binding_for(name)
        if not result['pidfd_open_binding'] or not result['pidfd_send_signal_binding']:
            result['runtime_probe'] = 'binding-unavailable'
            result['error_category'] = 'binding_unavailable'
            if not abi_ok:
                result['syscall_fallback_note'] = 'Only native Linux x86_64 LP64 is reviewed'
            return result

        result['probe_stage'] = 'pidfd_open_self'
        result['kernel_probe_attempted'] = True
        fd = _pidfd_open(os.getpid())
        result['probe_stage'] = 'pidfd_send_signal_self_0'
        _pidfd_send_signal(fd, 0)
        result['probe_stage'] = 'pidfd_pollability'
        # Use the exact select-based primitive used by terminate(). The caller
        # is alive, so its pidfd must not be ready for process-exit notification.
        if any(select.select([fd], [], [], 0)):
            raise Refuse('Self pidfd unexpectedly ready')
        result['usable'] = True
        result['runtime_probe'] = 'pass'
        result['probe_stage'] = 'complete'
    except Exception as exc:
        result['runtime_probe'] = f'{type(exc).__name__}: {exc}'
        result['error_type'] = type(exc).__name__
        if isinstance(exc, OSError):
            result['error_errno'] = exc.errno
            result['error_name'] = errno.errorcode.get(exc.errno, 'UNKNOWN')
            result['error_category'] = {
                errno.ENOSYS: 'syscall_unavailable_or_filtered',
                errno.EPERM: 'operation_not_permitted',
                errno.EACCES: 'permission_denied',
            }.get(exc.errno, 'runtime_error')
        else:
            result['error_category'] = 'runtime_or_binding_error'
    finally:
        if fd is not None:
            os.close(fd)
    return result


def listeners(port: int) -> list[dict]:
    ss = shutil.which('ss')
    if not ss: raise Refuse('ss is required')
    try:
        r = subprocess.run([ss, '-H', '-ltnp', f'sport = :{port}'], capture_output=True,
                           text=True, timeout=3, check=False, env={**os.environ,'LC_ALL':'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise Refuse(f':{port} listener lookup UNKNOWN (ss error/timeout)') from None
    if r.returncode or r.stderr.strip(): raise Refuse(f':{port} listener lookup UNKNOWN')
    out = []
    for line in r.stdout.splitlines():
        if not line.strip(): continue
        fields = line.split()
        if len(fields) < 4: raise Refuse('Unexpected ss output')
        out.append({'local':fields[3], 'pids':sorted(set(map(int,re.findall(r'pid=(\d+)',line))))})
    return out

def free_port(port: int) -> None:
    if port in PROTECTED or port == 8200: raise Refuse(f'Refusing shadow bind to protected/candidate :{port}')
    if listeners(port): raise Refuse(f':{port} occupied; never reusing an unrelated listener')

def proc_start(pid: int) -> tuple[str,str]:
    raw = Path(f'/proc/{pid}/stat').read_text()
    rest = raw[raw.rfind(')')+2:].split()
    return rest[19], rest[0]  # starttime field 22, process state field 3

def inspect_pid(pid: int) -> dict:
    p = Path(f'/proc/{pid}')
    begin,_ = proc_start(pid)
    argv = [x.decode(errors='surrogateescape') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
    env = {}
    for item in (p/'environ').read_bytes().split(b'\0'):
        if b'=' in item:
            k,v=item.split(b'=',1); env[k.decode(errors='surrogateescape')]=v.decode(errors='surrogateescape')
    result = {'pid':pid, 'start_ticks':begin, 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'exe':str((p/'exe').resolve(strict=True)), 'cwd':str((p/'cwd').resolve(strict=True)),
              'argv':argv, 'env':env, 'uid':p.stat().st_uid,
              'stdout':os.readlink(p/'fd/1'), 'stderr':os.readlink(p/'fd/2')}
    if proc_start(pid)[0] != begin: raise Refuse('PID changed during capture')
    return result

def capture(port: int) -> dict:
    ls = listeners(port)
    if not ls: raise Refuse(f'Expected existing :{port} is not listening; not guessing its state')
    if any(not l['pids'] for l in ls): raise Refuse(f':{port} listener owner unavailable; need service-owner permissions')
    pids = set(p for l in ls for p in l['pids'])
    if len(pids) != 1: raise Refuse(f':{port} has multiple listener owners; single-owner lifecycle required')
    record=inspect_pid(pids.pop()); record['port']=port; record['listeners']=ls
    return record

def alive(record: dict) -> bool:
    try:
        ticks, st = proc_start(record['pid'])
        return ticks == record['start_ticks'] and st != 'Z' and Path('/proc/sys/kernel/random/boot_id').read_text().strip() == record['boot_id']
    except (FileNotFoundError, ProcessLookupError): return False

def safe_summary(record: dict) -> dict:
    from common import redact
    argv=[];hide=False
    for item in record['argv']:
        if hide:argv.append('<REDACTED>');hide=False;continue
        if item.startswith('-') and re.search(r'(?i)(api[-_]key|token|secret|password|credential)',item):
            if '=' in item:argv.append(item.split('=',1)[0]+'=<REDACTED>')
            else:argv.append(item);hide=True
        else:argv.append(redact(item))
    return {'pid':record['pid'], 'start_ticks':record['start_ticks'], 'port':record.get('port'),
            'exe':record['exe'], 'cwd':record['cwd'], 'argv':argv,
            'env_names':sorted(record['env']), 'env_digest':sha_bytes(repr(sorted(record['env'].items())).encode())}

def verify_original(original: dict) -> dict:
    current=capture(original['port'])
    errors=[]
    for key in ('pid','start_ticks','boot_id','exe','cwd','argv','env','stdout','stderr'):
        if current[key] != original[key]: errors.append(key)
    return {'pass':not errors,'port':original['port'], 'same_pid':current['pid']==original['pid'], 'changed_fields':errors}

def set_flag(argv: list[str], flag: str, value: str) -> list[str]:
    out=[]; count=0; i=0
    while i < len(argv):
        item=argv[i]
        if item == flag:
            if i+1 >= len(argv) or argv[i+1].startswith('--'): raise Refuse(f'Missing value for {flag}')
            out.extend([flag,value]); i+=2; count+=1
        elif item.startswith(flag+'='):
            out.append(flag+'='+value); i+=1; count+=1
        else: out.append(item); i+=1
    if count>1: raise Refuse(f'Duplicate {flag}')
    if not count: out.extend([flag,value])
    return out

def flag_value(argv: list[str], flag: str):
    found=[]
    for i,item in enumerate(argv):
        if item==flag:
            if i+1==len(argv): raise Refuse('Missing flag value')
            found.append(argv[i+1])
        elif item.startswith(flag+'='): found.append(item[len(flag)+1:])
    if len(found)>1: raise Refuse('Duplicate CLI flag')
    return found[0] if found else None

def identity_only(record: dict) -> dict:
    return {k:record[k] for k in ('pid','start_ticks','boot_id','exe','cwd','argv','uid')}

def descendants(pid: int) -> list[dict]:
    # /proc task children avoids scanning all descriptors on the shared host.
    out=[]; seen={pid}; todo=[pid]
    while todo:
        parent=todo.pop()
        try:
            children=set()
            for task in Path(f'/proc/{parent}/task').iterdir():
                try: children.update(map(int,(task/'children').read_text().split()))
                except (OSError, ValueError): pass
            for child in children-seen:
                seen.add(child)
                try: out.append(identity_only(inspect_pid(child))); todo.append(child)
                except OSError: pass
        except OSError: pass
    return out

def _redis_title_check(record: dict, now: dict) -> dict:
    """Recognize only the standalone loopback title derived from our launch.

    Redis 6.0.16 calls redisSetProcTitle(argv[0]); argv[0] can be the
    ABSOLUTE launch path. The old basename-only exception rejected this.
    No fuzzy name match and no arbitrary argv replacement is accepted.
    PID/starttime/boot/exe/cwd/uid checks are performed separately below.
    """
    rejected = {'pass': False, 'rule': 'ARGV_MISMATCH'}
    if record.get('service_kind') != 'redis':
        return rejected
    port = record.get('port')
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1024 <= port <= 65535 or port in PROTECTED):
        return rejected
    # New records retain Popen argv. Old journals normally captured this same
    # two-element pre-title argv; allow recovery from that exact old format.
    launch = record.get('launch_argv', record.get('argv'))
    if (not isinstance(launch, list) or len(launch) != 2
            or any(not isinstance(x, str) or not x for x in launch)):
        return rejected
    executable, config = map(Path, launch)
    if (not executable.is_absolute() or executable.name != 'redis-server'
            or not config.is_absolute() or config.name != 'redis.conf'
            or str(config.parent) != record.get('cwd')):
        return rejected
    try:
        # Debian can expose /proc/PID/exe as redis-check-rdb because the
        # installed redis-server launcher is a symlink. Compare resolved paths.
        if str(executable.resolve(strict=True)) != record.get('exe'):
            return rejected
    except OSError:
        return rejected
    suffix = f' 127.0.0.1:{port}'
    allowed = ([launch[0] + suffix], ['redis-server' + suffix])
    if now.get('argv') not in allowed:
        return rejected
    return {'pass': True, 'rule': 'REDIS_STANDALONE_LOOPBACK_TITLE',
            'observed_title': now['argv'][0],
            'launch_binding': 'launch_argv' if 'launch_argv' in record else 'legacy_recorded_argv'}



def _complete_argv(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(x, str) and bool(x) for x in value))


def _empty_capture_launch_check(record: dict, now: dict) -> dict:
    """Recovery of an EMPTY initial cmdline, not authorization of an argv change.

    The old start_owned kept its exact Popen argument list as launch_argv even
    if its one /proc read returned argv=[]. Only that recorded list may repair
    this comparison. Never infer the expected list from the live process.
    All immutable identity fields are checked by owned_stop_check first.
    """
    rejected = {'pass': False, 'rule': 'ARGV_MISMATCH'}
    if record.get('argv') != [] or record.get('service_kind') not in {'backend', 'proxy', 'gateway'}:
        return rejected
    launch = record.get('launch_argv')
    if (not _complete_argv(launch) or len(launch) != 5
            or launch[1:3] != ['-I', '-B'] or now.get('argv') != launch):
        return rejected
    exe, bootstrap, spec = Path(launch[0]), Path(launch[3]), Path(launch[4])
    cwd = Path(record.get('cwd', ''))
    if (not all(p.is_absolute() for p in (exe, bootstrap, spec, cwd))
            or bootstrap.name != 'shadow_bootstrap.py'
            or spec.name != record['service_kind'] + '-launch.json'
            or spec.parent.parent.name != '.state'):
        return rejected
    try:
        # The launch spec and working directory belong to the SAME private run;
        # script/exe and argv match exactly, with no substituted shell/arguments.
        if str(exe.resolve(strict=True)) != record['exe']:
            return rejected
        if not bootstrap.is_file() or bootstrap.is_symlink():
            return rejected
        if spec.resolve(strict=True) != spec or cwd.resolve(strict=True) != cwd:
            return rejected
        if not spec.is_file() or not cwd.is_dir():
            return rejected
        cwd.relative_to(spec.parent)
    except (OSError, ValueError):
        return rejected
    port = record.get('port')
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1024 <= port <= 65535 or port in PROTECTED):
        return rejected
    try:
        rows = listeners(port)
    except Refuse:
        return {'pass': False, 'rule': 'EMPTY_CAPTURE_LISTENER_UNKNOWN'}
    if (not rows or any(row.get('local') != f'127.0.0.1:{port}'
                       or set(row.get('pids', [])) != {record['pid']} for row in rows)):
        return {'pass': False, 'rule': 'EMPTY_CAPTURE_LISTENER_OWNER_MISMATCH'}
    log = record.get('log')
    if not log or now.get('stdout') != log or now.get('stderr') != log:
        return {'pass': False, 'rule': 'EMPTY_CAPTURE_LOG_BINDING_MISMATCH'}
    import json
    return {'pass': True, 'rule': 'EMPTY_CAPTURE_EXACT_LAUNCH_ARGV',
            'launch_binding': 'recorded_Popen_launch_argv',
            'launch_argc': len(launch),
            'launch_argv_sha256': sha_bytes(json.dumps(launch, ensure_ascii=True).encode()),
            'initial_cmdline_was_empty': True,
            'scope': 'empty capture only; nonempty argv changes remain refused'}


def _capture_owned_identity(process, *, timeout: float = 3.0) -> tuple[dict, dict]:
    """Require two consecutive complete identity/cmdline observations.

    This is a bounded local metadata retry, NOT a restart, request replay or
    permit to replace the expected argv with later arbitrary live arguments.
    A persistent empty cmdline never becomes the normal lifecycle baseline.
    """
    deadline = time.monotonic() + timeout
    previous = None
    attempts = incomplete = 0
    while True:
        if process.poll() is not None:
            raise Refuse('Owned child exited before a stable identity could be captured')
        attempts += 1
        try:
            current = identity_only(inspect_pid(process.pid))
        except OSError:
            current = None
        if current is None or not _complete_argv(current.get('argv')):
            incomplete += 1
            previous = None
        elif current == previous:
            return current, {'rule': 'TWO_CONSECUTIVE_NONEMPTY_CMDLINE_READS',
                             'attempts': attempts, 'incomplete_reads': incomplete}
        else:
            previous = current
        if time.monotonic() >= deadline:
            raise Refuse('Owned identity capture timed out: incomplete/unstable cmdline; baseline not accepted')
        time.sleep(.02)


def owned_stop_check(record: dict, now: dict) -> dict:
    """Metadata-only authorization check; never sends a signal.

    The approved Redis title still requires the exact loopback listener owner.
    Normal processes require exact captured argv. Empty legacy captures may
    match only the originally journaled Popen argv with additional checks.
    """
    import json
    result = {'pass': False, 'pid': record.get('pid'), 'port': record.get('port'),
              'service_kind': record.get('service_kind'), 'rule': None,
              'expected_argc': len(record.get('argv', [])),
              'observed_argc': len(now.get('argv', [])),
              'expected_argv_sha256': sha_bytes(json.dumps(record.get('argv', []), ensure_ascii=True).encode()),
              'observed_argv_sha256': sha_bytes(json.dumps(now.get('argv', []), ensure_ascii=True).encode())}
    if record.get('port') in PROTECTED:
        result['rule'] = 'PROTECTED_PORT'; return result
    fields = ('pid', 'start_ticks', 'boot_id', 'exe', 'cwd', 'uid')
    changed = [k for k in fields if k not in record or k not in now or now[k] != record[k]]
    if changed:
        result.update(rule='IDENTITY_MISMATCH', changed_fields=changed); return result
    if not _complete_argv(now.get('argv')):
        result['rule'] = 'EMPTY_OR_INVALID_CURRENT_ARGV'; return result
    if now.get('argv') == record.get('argv'):
        result.update({'pass': True, 'rule': 'EXACT_ARGV'})
        return result
    if record.get('argv') == []:
        recovery = _empty_capture_launch_check(record, now)
        if recovery['rule'] != 'ARGV_MISMATCH':
            result.update(recovery); return result
    title = _redis_title_check(record, now)
    if not title['pass']:
        result['rule'] = 'ARGV_MISMATCH'; return result
    result.update(title)
    try:
        rows = listeners(record['port'])
    except Refuse:
        result.update({'pass': False, 'rule': 'REDIS_LISTENER_UNKNOWN'}); return result
    if (not rows or any(r.get('local') != f'127.0.0.1:{record["port"]}'
                        or set(r.get('pids', [])) != {record['pid']} for r in rows)):
        result.update({'pass': False, 'rule': 'REDIS_LISTENER_OWNER_MISMATCH'}); return result
    # Rewriting argv must not waive the original stdout/stderr binding either.
    expected_log = record.get('log')
    if (not expected_log or now.get('stdout') != expected_log
            or now.get('stderr') != expected_log):
        result.update({'pass': False, 'rule': 'REDIS_LOG_BINDING_MISMATCH'}); return result
    return result


def inspect_owned_state(state: Path) -> dict:
    """Read-only journal/PID comparison. No HTTP/Redis request and no signal."""
    raw_state = state.absolute()
    if raw_state.is_symlink() or raw_state.resolve(strict=True) != raw_state:
        raise Refuse('Recovery state path must not contain symlinks')
    journal_path = raw_state/'journal.json'
    if journal_path.is_symlink(): raise Refuse('Recovery journal must not be a symlink')
    journal = read_json(journal_path)
    if journal.get('marker') != 'BLINK_CONCURRENT_SHADOW' or journal.get('state_dir') != str(raw_state):
        raise Refuse('Not a compatible concurrent-shadow journal')
    checks = []
    for name, rec in sorted(journal.get('owned', {}).items()):
        if rec.get('port') in PROTECTED:
            checks.append({'name': name, 'pass': False, 'rule': 'PROTECTED_PORT'}); continue
        try:
            if not alive(rec):
                check = {'pass': True, 'rule': 'ORIGINAL_PROCESS_ALREADY_GONE', 'pid': rec.get('pid'), 'port': rec.get('port')}
            else:
                check = owned_stop_check(rec, inspect_pid(rec['pid']))
        except (OSError, Refuse, KeyError, ValueError) as exc:
            check = {'pass': False, 'rule': 'INSPECTION_UNAVAILABLE', 'error_type': type(exc).__name__}
        checks.append({'name': name, **check})
    return {'pass': all(c['pass'] for c in checks), 'checks': checks,
            'signals_sent': 0, 'service_actions': [],
            'scope': 'read-only observation; recovery revalidates identity via pidfd before SIGTERM'}


def terminate(record: dict, *, timeout: float, owned: bool=False, candidate: bool=False) -> None:
    if candidate or not owned:
        raise Refuse('Only journaled benchmark children may be signalled; shared candidates are read-only')
    if record.get('port') in PROTECTED: raise Refuse('Production signal prohibited')
    if not alive(record): return
    capability=pidfd_capability()
    if not capability['usable']:
        raise Refuse('Linux pidfd lifecycle unavailable: '+capability['runtime_probe'])
    fd=_pidfd_open(record['pid'])
    try:
        if not alive(record): return
        # Match executable/cwd again. pidfd keeps the signal bound to the opened process.
        now=inspect_pid(record['pid'])
        if any(now[k] != record[k] for k in ('pid','start_ticks','boot_id','exe','cwd','uid')):
            raise Refuse('Process identity changed before stop')
        check=owned_stop_check(record,now)
        record['last_stop_check']=check
        if not check['pass']:
            raise Refuse('Owned stop check refused: '+check['rule']+
                         f' (kind={record.get("service_kind")}, pid={record["pid"]}, port={record.get("port")}); '+
                         'inspect --owned-state-check; no signal sent')
        _pidfd_send_signal(fd,signal.SIGTERM)
        if not select.select([fd],[],[],timeout)[0]:
            raise Refuse('Graceful stop timed out; no SIGKILL sent')
    finally: os.close(fd)

def start_owned(argv: list[str], cwd: Path, env: dict, log: Path, port: int,
                journal: dict, journal_file: Path, name: str) -> subprocess.Popen:
    if port in PROTECTED: raise Refuse('Production bind prohibited')
    if listeners(port): raise Refuse(f':{port} became occupied before launch')
    log.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with log.open('ab',buffering=0) as f:
        p=subprocess.Popen(argv,cwd=str(cwd),env=env,stdin=subprocess.DEVNULL,
                           stdout=f,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
    try:
        rec,capture_proof=_capture_owned_identity(p)
        rec.update(port=port,log=str(log),service_kind=name,launch_argv=list(argv),
                   identity_capture=capture_proof)
    except Exception:
        if p.poll() is None:
            fd = _pidfd_open(p.pid)
            try:
                _pidfd_send_signal(fd, signal.SIGTERM)
                if not select.select([fd], [], [], 10)[0]:
                    raise Refuse('New child identity capture failed and child did not exit; inspect new-child failure')
            finally: os.close(fd)
            p.wait(timeout=2)
        raise
    journal.setdefault('owned',{})[name]=rec; atomic_json(journal_file,journal)
    return p

def stop_owned(journal: dict, journal_file: Path, name: str, timeout=90) -> None:
    rec=journal.get('owned',{}).get(name)
    if not rec: return
    prior=rec.get('descendants',[])
    observed=descendants(rec['pid']) if alive(rec) else []
    by_id={(r['pid'],r['start_ticks'],r['boot_id']):r for r in prior+observed}
    children=list(by_id.values())
    rec['descendants']=children; atomic_json(journal_file,journal)
    try:
        terminate(rec,timeout=timeout,owned=True)
    finally:
        if 'last_stop_check' in rec:
            from common import utc
            journal.setdefault('owned_stop_checks', []).append(
                {'name': name, 'timestamp': utc(), **rec.pop('last_stop_check')})
            atomic_json(journal_file,journal)
    # Children captured while the owned parent is alive, not arbitrary port owners.
    for ch in reversed(children):
        if alive(ch): terminate(ch,timeout=timeout,owned=True)
    occupants=listeners(rec['port'])
    if occupants:
        # An external process can take the released port. Never stop it; record
        # the collision without calling it failure to clean our already-dead child.
        ours={r['pid'] for r in [rec,*children] if alive(r)}
        if any(set(row['pids']) & ours for row in occupants):
            raise Refuse('Owned process still holds listener after graceful cleanup')
        journal.setdefault('external_port_replacements',[]).append({'port':rec['port'], 'observed_pids':sorted({p for r in occupants for p in r['pids']}), 'action':'none'})
    journal['owned'].pop(name,None); atomic_json(journal_file,journal)


def stop_candidate(*args, **kwargs):
    raise Refuse('Concurrent-deploy mode never stops or restores shared :8200')


def update_candidate_pidfiles(*args, **kwargs):
    raise Refuse('Concurrent-deploy mode never writes shared candidate PID files')


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(
        description='Read-only pidfd or owned-journal check; no service actions.'
    )
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--pidfd-check', action='store_true')
    group.add_argument('--owned-state-check', type=Path, metavar='PRIVATE_STATE_DIR')
    args=parser.parse_args()
    if args.owned_state_check:
        try: report=inspect_owned_state(args.owned_state_check)
        except (Refuse, OSError, ValueError) as exc:
            from common import redact
            report={'pass':False,'error_type':type(exc).__name__,'message':redact(str(exc)), 'signals_sent':0}
        print(json.dumps(report,ensure_ascii=False,indent=2))
        raise SystemExit(0 if report['pass'] else 2)
    capability = pidfd_capability()
    print(json.dumps(capability, ensure_ascii=False, indent=2))
    raise SystemExit(0 if capability['usable'] else 1)
