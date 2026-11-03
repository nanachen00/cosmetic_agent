#!/usr/bin/env python3
"""Manage only the canonical one-worker, localhost:8010 BLINK backend."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path("/var/lib/jenkins/tomod")
PYTHON = Path("/opt/conda/envs/blink-backend/bin/python")
PORT = 8010
SERVICE = "blink-managed-knowledge-backend-v1"


class LifecycleError(Exception):
    pass


def _libc_pidfd(name, argument_types):
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError:
        raise LifecycleError("pidfd_signaling_unavailable") from None
    try:
        function = getattr(library, name)
    except AttributeError:
        # Linux x86-64 ABI only. Older glibc has no pidfd wrappers, even when
        # the kernel supports them. Verified upstream syscall assignments:
        # https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/entry/syscalls/syscall_64.tbl
        if (sys.platform != "linux" or platform.machine() != "x86_64"
                or ctypes.sizeof(ctypes.c_void_p) != 8):
            raise LifecycleError("pidfd_signaling_unavailable") from None
        try:
            syscall = library.syscall
        except AttributeError:
            raise LifecycleError("pidfd_signaling_unavailable") from None
        syscall.restype = ctypes.c_long
        if name == "pidfd_open":
            return lambda pid, flags: syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(flags))
        if name == "pidfd_send_signal":
            return lambda fd, signo, info, flags: syscall(
                ctypes.c_long(424), ctypes.c_int(fd), ctypes.c_int(signo),
                ctypes.c_void_p(info), ctypes.c_uint(flags))
        raise LifecycleError("pidfd_signaling_unavailable") from None
    function.argtypes, function.restype = argument_types, ctypes.c_int
    return function


def _pidfd_result(result):
    if result < 0:
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            raise ProcessLookupError(error, "process_exited")
        if error in (errno.ENOSYS, errno.ENOTSUP):
            raise LifecycleError("pidfd_signaling_unavailable")
        raise LifecycleError("pidfd_operation_failed")
    return result


def open_pidfd(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    function = _libc_pidfd("pidfd_open", [ctypes.c_int, ctypes.c_uint])
    return _pidfd_result(function(pid, 0))


def send_pidfd_term(descriptor):
    if hasattr(signal, "pidfd_send_signal"):
        return signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    function = _libc_pidfd("pidfd_send_signal", [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint])
    return _pidfd_result(function(descriptor, signal.SIGTERM, None, 0))


class Manager:
    def __init__(self, root=ROOT, *, proc_root=Path("/proc")):
        self.root, self.proc = Path(root).resolve(), Path(proc_root)
        self.backend = self.root / "cosmetic-agent"
        self.runtime = self.root / "ops/runtime"
        self.pids, self.logs = self.runtime / "pids", self.runtime / "logs"
        self.pid_path = self.pids / "backend.pid"
        self.identity_path = self.pids / "backend.identity.json"
        self.command = [str(PYTHON), "-m", "uvicorn", "api.api_server:app", "--host",
                        "127.0.0.1", "--port", "8010", "--workers", "1"]

    @contextlib.contextmanager
    def lock(self):
        if self.backend.is_symlink() or not self.backend.is_dir():
            raise LifecycleError("canonical_backend_missing")
        for directory in (self.root / "ops", self.runtime, self.pids, self.logs):
            if directory.is_symlink():
                raise LifecycleError("runtime_path_is_symlink")
            directory.mkdir(mode=0o750, exist_ok=True)
        descriptor = os.open(self.pids / "backend.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LifecycleError("management_busy") from None
                    time.sleep(.05)
            yield
        finally:
            os.close(descriptor)

    def identity(self, pid):
        directory = self.proc / str(pid)
        try:
            text = (directory / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return {"pid": pid, "start_ticks": fields[19], "state": "Z"}
            return {"pid": pid, "start_ticks": fields[19], "state": fields[0],
                    "uid": directory.stat().st_uid,
                    "cwd": str(Path(os.readlink(directory / "cwd")).resolve()),
                    "exe": str(Path(os.readlink(directory / "exe")).resolve()),
                    "args": (directory / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")}
        except FileNotFoundError:
            if not directory.exists():
                return None
            raise LifecycleError("process_identity_unverifiable") from None
        except (OSError, UnicodeError, IndexError, ValueError):
            raise LifecycleError("process_identity_unverifiable") from None

    def exact(self, identity):
        return identity is not None and identity["state"] != "Z" and identity["uid"] == os.getuid() and (
            identity["args"] == self.command and identity["cwd"] == str(self.backend.resolve())
            and identity["exe"] == str(PYTHON.resolve()))

    def listeners(self):
        found = {}
        for filename in ("tcp", "tcp6"):
            try:
                rows = (self.proc / "net" / filename).read_text().splitlines()[1:]
            except FileNotFoundError:
                if filename == "tcp6":
                    continue
                raise LifecycleError("listener_inspection_unavailable") from None
            except OSError:
                raise LifecycleError("listener_inspection_unavailable") from None
            for row in rows:
                try:
                    fields = row.split()
                    address, port = fields[1].split(":")
                    if fields[3] == "0A" and int(port, 16) == PORT:
                        found[fields[9]] = (filename, address)
                except (IndexError, ValueError):
                    raise LifecycleError("listener_inspection_invalid") from None
        return found

    def owns_socket(self, pid, listeners):
        if not listeners or any(value != ("tcp", "0100007F") for value in listeners.values()):
            return False
        try:
            sockets = set()
            for fd in (self.proc / str(pid) / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    sockets.add(target[8:-1])
            return set(listeners).issubset(sockets)
        except OSError:
            raise LifecycleError("listener_owner_unverifiable") from None

    def record(self):
        candidates = [self.pid_path, self.pids / "backend-8010.pid"]
        for path in candidates + [self.identity_path]:
            if path.is_symlink():
                raise LifecycleError("pid_record_is_symlink")
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            if self.identity_path.exists():
                raise LifecycleError("orphan_identity_record")
            return None
        raw = source.read_text().strip()
        if len(raw) > 32 or not raw.isdigit() or int(raw) <= 1:
            raise LifecycleError("invalid_pid_record")
        pid = int(raw)
        if not self.identity_path.exists():
            return {"pid": pid}
        try:
            data = self.identity_path.read_text()
            if len(data) > 4096:
                raise ValueError()
            record = json.loads(data)
            if (not isinstance(record, dict) or record.get("service") != SERVICE
                    or type(record.get("pid")) is not int or record["pid"] != pid
                    or not str(record.get("start_ticks", "")).isdigit()):
                raise ValueError()
            return record
        except (ValueError, UnicodeError):
            raise LifecycleError("invalid_identity_record") from None

    def current(self):
        record, listeners = self.record(), self.listeners()
        if record:
            identity = self.identity(record["pid"])
            if identity is not None and identity["state"] != "Z":
                if not self.exact(identity) or (
                    "start_ticks" in record and record["start_ticks"] != identity["start_ticks"]
                ):
                    raise LifecycleError("pid_identity_mismatch")
                if listeners and not self.owns_socket(identity["pid"], listeners):
                    raise LifecycleError("port_owned_by_unknown_process")
                if "start_ticks" not in record and not listeners:
                    raise LifecycleError("legacy_pid_not_listening")
                return identity
        if not listeners:
            return None
        matches = []
        for directory in self.proc.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if directory.stat().st_uid != os.getuid():
                    continue
                identity = self.identity(int(directory.name))
                if self.exact(identity) and self.owns_socket(identity["pid"], listeners):
                    matches.append(identity)
            except LifecycleError:
                continue
            except FileNotFoundError:
                continue
        if len(matches) != 1:
            raise LifecycleError("port_owned_by_unknown_process")
        return matches[0]

    def write_record(self, identity):
        values = ((self.pid_path, str(identity["pid"]) + "\n"),
                  (self.identity_path, json.dumps({"service": SERVICE, "pid": identity["pid"],
                                                 "start_ticks": identity["start_ticks"]}) + "\n"))
        for path, data in values:
            if path.is_symlink():
                raise LifecycleError("pid_record_is_symlink")
            with tempfile.NamedTemporaryFile(mode="w", dir=self.pids, delete=False) as handle:
                handle.write(data)
                temporary = handle.name
            os.replace(temporary, path)

    def environment(self, expected_release=None):
        from dotenv import dotenv_values
        path = self.backend / ".env"
        if path.is_symlink() or not path.is_file():
            raise LifecycleError("canonical_env_missing")
        try:
            values = dotenv_values(path, interpolate=False)
        except Exception:
            raise LifecycleError("canonical_env_unreadable") from None
        environment = {"PATH": f"{PYTHON.parent}:/usr/local/bin:/usr/bin:/bin",
                       "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1"}
        if os.environ.get("HOME"):
            environment["HOME"] = os.environ["HOME"]
        environment.update({key: value for key, value in values.items() if value is not None})
        if expected_release is not None and environment.get("APP_RELEASE") != expected_release:
            raise LifecycleError("app_release_mismatch")
        return environment

    def start(self, expected_release=None):
        environment = self.environment(expected_release)
        identity = self.current()
        if identity:
            if expected_release is not None:
                try:
                    values = (self.proc / str(identity["pid"]) / "environ").read_bytes().split(b"\0")
                    release = next((item[12:].decode() for item in values if item.startswith(b"APP_RELEASE=")), None)
                except (OSError, UnicodeError):
                    raise LifecycleError("running_release_unverifiable") from None
                if release != expected_release:
                    raise LifecycleError("running_release_mismatch")
            self.write_record(identity)
            return {"status": "already_running", "pid": identity["pid"]}
        log_path = self.logs / "backend.log"
        if log_path.is_symlink():
            raise LifecycleError("log_path_is_symlink")
        descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "ab") as log:
            child = subprocess.Popen(self.command, cwd=self.backend, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        # Return without waiting for application initialization or readiness.
        identity = self.identity(child.pid)
        if not self.exact(identity):
            raise LifecycleError("started_process_identity_unverifiable")
        self.write_record(identity)
        return {"status": "starting", "pid": child.pid}

    def stop(self, deadline=None):
        identity = self.current()
        if identity is None:
            return {"status": "stopped"}
        self.write_record(identity)
        try:
            descriptor = open_pidfd(identity["pid"])
        except ProcessLookupError:
            raise LifecycleError("process_exited_retry_status") from None
        try:
            fresh = self.identity(identity["pid"])
            if not self.exact(fresh) or fresh["start_ticks"] != identity["start_ticks"]:
                raise LifecycleError("pid_identity_changed")
            try:
                send_pidfd_term(descriptor)
            except ProcessLookupError:
                pass  # Exited after identity verification; never signal a new PID.
        finally:
            os.close(descriptor)
        limit = deadline if deadline is not None else time.monotonic() + 30
        while time.monotonic() < limit:
            fresh = self.identity(identity["pid"])
            if fresh is None or fresh["state"] == "Z":
                for path in (self.pid_path, self.identity_path):
                    if path.is_symlink():
                        raise LifecycleError("pid_record_is_symlink")
                    path.unlink(missing_ok=True)
                return {"status": "stopped", "pid": identity["pid"]}
            if not self.exact(fresh) or fresh["start_ticks"] != identity["start_ticks"]:
                raise LifecycleError("pid_identity_changed")
            time.sleep(.1)
        raise LifecycleError("termination_timeout_no_kill_sent")

    def run(self, action, expected_release=None):
        if action not in {"status", "start", "stop", "restart"}:
            raise LifecycleError("invalid_action")
        if expected_release is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", expected_release):
            raise LifecycleError("invalid_expected_release")
        with self.lock():
            if action == "status":
                identity = self.current()
                return {"status": "running" if identity and self.owns_socket(identity["pid"], self.listeners())
                        else "starting" if identity else "stopped", "pid": identity["pid"] if identity else None}
            if action == "stop":
                return self.stop()
            if action == "restart":
                self.environment(expected_release)  # Fail before stopping if release/env is wrong.
                self.stop(deadline=time.monotonic() + 30)
            return self.start(expected_release)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start", "stop", "restart"))
    parser.add_argument("--expected-release")
    args = parser.parse_args()
    try:
        print(json.dumps(Manager().run(args.action, args.expected_release)))
    except LifecycleError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": "lifecycle_failed", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
