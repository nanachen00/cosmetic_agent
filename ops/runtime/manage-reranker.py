#!/usr/bin/env python3
"""Manage only the canonical, single-worker localhost:8006 reranker.

Install this helper under /var/lib/jenkins/tomod/ops/runtime/. Start is
nonblocking; use status again after model loading. Stop sends TERM, never KILL.
No platform restart, port 8200 operation, model download, or .env write occurs.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request


ROOT = Path("/var/lib/jenkins/tomod")
RUNTIME_PYTHON = Path("/opt/conda/envs/py_3.10/bin/python")
ENV_PYTHON = Path("/opt/conda/envs/blink-backend/bin/python")
MODEL = "BAAI/bge-reranker-v2-m3"
PORT = 8006
SERVICE = "blink-managed-reranker-v1"
ENV_NAMES = (
    "RERANKER_MODEL_NAME", "RERANKER_DEVICE", "RERANKER_MAX_DOCUMENTS",
    "RERANKER_MAX_DOCUMENT_CHARS", "RERANKER_MAX_CONCURRENT",
    "RERANKER_MAX_QUEUED", "RERANKER_QUEUE_TIMEOUT_SECONDS",
    "RERANKER_INFERENCE_TIMEOUT_SECONDS", "RERANKER_BATCH_SIZE", "APP_RELEASE",
)
DEFAULTS = {
    "RERANKER_MODEL_NAME": MODEL, "RERANKER_DEVICE": "cuda",
    "RERANKER_MAX_DOCUMENTS": "64", "RERANKER_MAX_DOCUMENT_CHARS": "4000",
    "RERANKER_MAX_CONCURRENT": "1", "RERANKER_MAX_QUEUED": "8",
    "RERANKER_QUEUE_TIMEOUT_SECONDS": "2",
    "RERANKER_INFERENCE_TIMEOUT_SECONDS": "30", "RERANKER_BATCH_SIZE": "8",
}


class LifecycleError(Exception):
    pass


class Manager:
    def __init__(self, root=ROOT, *, proc_root=Path("/proc")):
        # Root injection exists for unit tests, never as a CLI option.
        self.root = Path(root).resolve()
        self.proc_root = Path(proc_root)
        self.backend = self.root / "cosmetic-agent"
        self.runtime = self.root / "ops" / "runtime"
        self.pid_dir = self.runtime / "pids"
        self.log_dir = self.runtime / "logs"
        self.pid_path = self.pid_dir / "reranker.pid"
        self.identity_path = self.pid_dir / "reranker.identity.json"
        self.log_path = self.log_dir / "reranker.log"
        self.command = [str(RUNTIME_PYTHON), "-m", "uvicorn", "api_server_reranker:app",
                        "--host", "127.0.0.1", "--port", str(PORT), "--workers", "1"]

    def _prepare_dirs(self):
        if not self.backend.is_dir() or self.backend.is_symlink():
            raise LifecycleError("canonical_backend_missing")
        for directory in (self.root / "ops", self.runtime, self.pid_dir, self.log_dir):
            if directory.is_symlink():
                raise LifecycleError("runtime_path_is_symlink")
            directory.mkdir(mode=0o750, exist_ok=True)

    @contextlib.contextmanager
    def _lock(self):
        self._prepare_dirs()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.pid_dir / "reranker.lock", flags, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LifecycleError("management_busy") from None
            yield
        finally:
            os.close(descriptor)

    def _read_record(self):
        if self.pid_path.is_symlink() or self.identity_path.is_symlink():
            raise LifecycleError("pid_record_is_symlink")
        try:
            raw = self.pid_path.read_bytes()
        except FileNotFoundError:
            if self.identity_path.exists():
                raise LifecycleError("orphan_identity_record")
            return None
        if len(raw) > 32:
            raise LifecycleError("invalid_pid_record")
        try:
            numeric = raw.decode("ascii").strip()
            if not numeric.isdigit() or int(numeric) <= 1:
                raise ValueError()
            pid = int(numeric)
        except (ValueError, UnicodeError):
            raise LifecycleError("invalid_pid_record") from None
        try:
            metadata = self.identity_path.read_bytes()
        except FileNotFoundError:
            # Legacy plain PID files may only be cleared when conclusively dead
            # and no listener exists. Never adopt or signal an unowned process.
            if self._identity(pid) is None and not self._listeners():
                return {"service": SERVICE, "pid": pid, "start_ticks": "0", "port": PORT, "legacy": True}
            raise LifecycleError("legacy_pid_without_identity") from None
        try:
            if len(metadata) > 4096:
                raise ValueError()
            record = json.loads(metadata)
        except (ValueError, UnicodeError):
            raise LifecycleError("invalid_identity_record") from None
        if not isinstance(record, dict) or (
            record.get("service") != SERVICE
            or type(record.get("pid")) is not int or record["pid"] <= 1
            or not isinstance(record.get("start_ticks"), str)
            or not record["start_ticks"].isdigit()
            or record.get("port") != PORT
        ):
            raise LifecycleError("invalid_identity_record")
        if record["pid"] != pid:
            raise LifecycleError("pid_identity_mismatch")
        return record

    def _remove_record(self):
        self.pid_path.unlink()
        try:
            self.identity_path.unlink()
        except FileNotFoundError:
            pass

    def _identity(self, pid):
        directory = self.proc_root / str(pid)
        try:
            stat_text = (directory / "stat").read_text()
            fields = stat_text[stat_text.rfind(")") + 2:].split()
            return {
                "start_ticks": fields[19], "state": fields[0],
                "uid": directory.stat().st_uid,
                "cwd": Path(os.readlink(directory / "cwd")).resolve(),
                "exe": Path(os.readlink(directory / "exe")).resolve(),
                "args": (directory / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0"),
            }
        except FileNotFoundError:
            if not directory.exists():
                return None
            raise LifecycleError("process_identity_unverifiable") from None
        except (OSError, ValueError, IndexError, UnicodeError):
            raise LifecycleError("process_identity_unverifiable") from None

    def _is_owned(self, identity, record):
        return identity is not None and (
            identity["start_ticks"] == record["start_ticks"]
            and identity["uid"] == os.getuid()
            and identity["cwd"] == self.backend.resolve()
            and identity["exe"] == RUNTIME_PYTHON.resolve()
            and identity["args"] == self.command
        )

    def _listeners(self):
        listeners = {}
        for filename in ("tcp", "tcp6"):
            path = self.proc_root / "net" / filename
            try:
                rows = path.read_text().splitlines()[1:]
            except FileNotFoundError:
                if filename == "tcp6":
                    continue
                raise LifecycleError("listener_inspection_unavailable") from None
            except OSError:
                raise LifecycleError("listener_inspection_unavailable") from None
            for row in rows:
                fields = row.split()
                try:
                    address, port = fields[1].split(":")
                    if fields[3] == "0A" and int(port, 16) == PORT:
                        listeners[fields[9]] = (filename, address)
                except (ValueError, IndexError):
                    raise LifecycleError("listener_inspection_invalid") from None
        return listeners

    def _listeners_owned(self, pid, listeners):
        if not listeners:
            return True
        try:
            sockets = set()
            for descriptor in (self.proc_root / str(pid) / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    sockets.add(target[8:-1])
            return set(listeners).issubset(sockets) and all(
                value == ("tcp", "0100007F") for value in listeners.values()
            )
        except OSError:
            raise LifecycleError("listener_owner_unverifiable") from None

    def _health(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8006/readyz", timeout=1) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    return False
                data = json.loads(raw)
                return isinstance(data, dict) and data.get("ready") is True and (
                    data.get("model", "").split("/")[-1] == MODEL.split("/")[-1]
                )
        except Exception:
            return False

    def _load_env(self):
        dotenv_path = self.backend / ".env"
        if not dotenv_path.is_file() or dotenv_path.is_symlink():
            raise LifecycleError("canonical_env_missing")
        script = (
            "import json,sys; from dotenv import dotenv_values; "
            "values=dotenv_values(sys.argv[1],interpolate=False); "
            "names=json.loads(sys.argv[2]); "
            "print(json.dumps({k:values[k] for k in names if values.get(k) is not None}))"
        )
        try:
            completed = subprocess.run(
                [str(ENV_PYTHON), "-c", script, str(dotenv_path), json.dumps(ENV_NAMES)],
                cwd=self.backend, capture_output=True, timeout=5, check=False,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
            )
            if completed.returncode or len(completed.stdout) > 65536:
                raise LifecycleError("env_reader_failed")
            selected = json.loads(completed.stdout)
            if not isinstance(selected, dict):
                raise LifecycleError("env_reader_failed")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise LifecycleError("env_reader_failed") from None
        settings = dict(DEFAULTS)
        settings.update({key: value for key, value in selected.items() if key in ENV_NAMES and value != ""})
        if settings["RERANKER_MODEL_NAME"] != MODEL:
            raise LifecycleError("model_not_allowed")
        if settings["RERANKER_DEVICE"] not in {"cuda", "cuda:0"}:
            raise LifecycleError("gpu_device_required")
        bounds = {
            "RERANKER_MAX_DOCUMENTS": (1, 64), "RERANKER_MAX_DOCUMENT_CHARS": (1, 4000),
            "RERANKER_MAX_CONCURRENT": (1, 1), "RERANKER_MAX_QUEUED": (0, 8),
            "RERANKER_BATCH_SIZE": (1, 8),
        }
        for key, (lower, upper) in bounds.items():
            value = settings[key]
            if not isinstance(value, str) or not value.isdigit() or not lower <= int(value) <= upper:
                raise LifecycleError("invalid_capacity_setting")
        for key, maximum in (("RERANKER_QUEUE_TIMEOUT_SECONDS", 2), ("RERANKER_INFERENCE_TIMEOUT_SECONDS", 30)):
            try:
                value = float(settings[key])
                if not 0 < value <= maximum:
                    raise ValueError()
            except (ValueError, TypeError):
                raise LifecycleError("invalid_timeout_setting") from None
        release = settings.get("APP_RELEASE", "")
        if release and (not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", release)):
            raise LifecycleError("invalid_release_setting")
        # Deliberately do not inherit API keys, arbitrary Python paths, or app flags.
        environment = {
            "PATH": "/opt/conda/envs/py_3.10/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
            "HF_HOME": "/data/blink/models/hf-cache", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
        environment.update(settings)
        return environment

    def _write_record(self, record):
        # Keep the legacy control-plane .pid format: a plain decimal PID.
        # Publish identity first; interruption between files fails closed as an
        # orphan/mismatch rather than allowing an unverified process operation.
        for destination, contents in (
            (self.identity_path, json.dumps(record)),
            (self.pid_path, str(record["pid"]) + "\n"),
        ):
            temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    handle.write(contents)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _spawn(self, environment):
        if not RUNTIME_PYTHON.is_file() or not (self.backend / "api_server_reranker.py").is_file():
            raise LifecycleError("reranker_runtime_missing")
        descriptor = os.open(self.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "ab", buffering=0) as logfile:
            return subprocess.Popen(
                self.command, cwd=self.backend, env=environment,
                stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )

    def start(self):
        # Parsing .env is outside the management lock; spawning never polls.
        environment = self._load_env()
        with self._lock():
            record = self._read_record()
            listeners = self._listeners()
            if record:
                identity = self._identity(record["pid"])
                if identity is not None:
                    if not self._is_owned(identity, record):
                        raise LifecycleError("pid_ownership_mismatch")
                    if not self._listeners_owned(record["pid"], listeners):
                        raise LifecycleError("port_in_use_unmanaged")
                    result = {"ok": True, "code": "already_managed", "pid": record["pid"], "state": "running" if listeners else "starting"}
                    # Actual health is queried after releasing the lock below.
                    existing = True
                else:
                    if listeners:
                        raise LifecycleError("port_in_use_unmanaged")
                    self._remove_record()
                    existing = False
            else:
                existing = False
            if not existing:
                if listeners:
                    raise LifecycleError("port_in_use_unmanaged")
                process = self._spawn(environment)
                identity = self._identity(process.pid)
                if identity is None or process.poll() is not None:
                    raise LifecycleError("process_exited_before_record")
                record = {"service": SERVICE, "pid": process.pid, "start_ticks": identity["start_ticks"], "port": PORT}
                if not self._is_owned(identity, record):
                    raise LifecycleError("spawn_identity_unverifiable")
                try:
                    self._write_record(record)
                except Exception:
                    # Only this newly spawned, still-identical process is eligible.
                    if self._is_owned(self._identity(process.pid), record):
                        os.kill(process.pid, signal.SIGTERM)
                    raise LifecycleError("pid_record_write_failed") from None
                result = {"ok": True, "code": "start_requested", "pid": process.pid, "state": "starting"}
        if existing:
            result["ready"] = bool(listeners) and self._health()
        else:
            result["ready"] = False
        return result

    def status(self):
        record = self._read_record()
        listeners = self._listeners()
        if not record:
            if listeners:
                raise LifecycleError("port_in_use_unmanaged")
            return {"ok": True, "code": "not_running", "state": "stopped", "ready": False}
        identity = self._identity(record["pid"])
        if identity is None:
            if listeners:
                raise LifecycleError("port_in_use_unmanaged")
            return {"ok": True, "code": "stale_pid_record", "state": "stopped", "ready": False}
        if not self._is_owned(identity, record):
            raise LifecycleError("pid_ownership_mismatch")
        if not self._listeners_owned(record["pid"], listeners):
            raise LifecycleError("port_in_use_unmanaged")
        healthy = bool(listeners) and self._health()
        return {"ok": True, "code": "ready" if healthy else "managed_not_ready", "pid": record["pid"], "state": "running" if listeners else "starting", "ready": healthy, "gpu_observed": None}

    def stop(self):
        with self._lock():
            record = self._read_record()
            listeners = self._listeners()
            if not record:
                if listeners:
                    raise LifecycleError("port_in_use_unmanaged")
                return {"ok": True, "code": "already_stopped", "state": "stopped"}
            identity = self._identity(record["pid"])
            if identity is None:
                if listeners:
                    raise LifecycleError("port_in_use_unmanaged")
                self._remove_record()
                return {"ok": True, "code": "stale_pid_removed", "state": "stopped"}
            if not self._is_owned(identity, record):
                raise LifecycleError("pid_ownership_mismatch")
            if not self._listeners_owned(record["pid"], listeners):
                raise LifecycleError("port_in_use_unmanaged")
            # Recheck PID start time immediately before TERM to detect PID reuse.
            if not self._is_owned(self._identity(record["pid"]), record):
                raise LifecycleError("pid_identity_changed")
            os.kill(record["pid"], signal.SIGTERM)
            return {"ok": True, "code": "term_requested", "pid": record["pid"], "state": "stopping", "forced": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    args = parser.parse_args(argv)
    try:
        result = getattr(Manager(), args.action)()
    except LifecycleError as error:
        result = {"ok": False, "code": str(error)}
    except Exception:
        result = {"ok": False, "code": "lifecycle_operation_failed"}
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
