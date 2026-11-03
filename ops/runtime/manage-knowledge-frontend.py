#!/usr/bin/env python3
"""Verified frontend-only activation/rollback; never builds or controls APIs."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
try:
    import knowledge_frontend_build as build
except ModuleNotFoundError:
    import prepare_frontend_build as build

NODE = Path("/opt/node-v22.23.4/bin/node")
PORT = 3000


class ActivationError(Exception):
    pass


class Manager:
    def __init__(self, root=build.ROOT, *, proc_root=Path("/proc"), compat_pid=Path("/tmp/frontend_3000.pid")):
        self.root, self.proc = Path(root).resolve(), Path(proc_root)
        self.builder = build.FrontendBuild(self.root)
        self.live, self.release = self.builder.live, self.builder.release
        self.archive = self.root / "ops/archive" / build.RELEASE / "frontend"
        self.runtime = self.root / "ops/runtime"
        self.pid_path, self.compat_pid = self.runtime / "pids/frontend.pid", Path(compat_pid)
        self.state_path = self.runtime / "knowledge-frontend-state.json"
        self.command = [str(NODE), str(self.live / "node_modules/next/dist/bin/next"),
                        "start", "--hostname", "0.0.0.0", "--port", "3000"]

    @contextlib.contextmanager
    def lock(self):
        for path in (self.root / "ops", self.runtime, self.runtime / "pids", self.runtime / "logs"):
            if path.is_symlink():
                raise ActivationError("runtime_symlink_refused")
            path.mkdir(mode=0o750, exist_ok=True)
        descriptor = os.open(self.runtime / "frontend-activation.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ActivationError("activation_busy") from None
            yield
        finally:
            os.close(descriptor)

    def identity(self, pid):
        directory = self.proc / str(pid)
        try:
            text = (directory / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            if fields[0] == "Z":
                return None
            return {"pid": pid, "start_ticks": fields[19], "uid": directory.stat().st_uid,
                    "cwd": str(Path(os.readlink(directory / "cwd")).resolve()),
                    "exe": str(Path(os.readlink(directory / "exe")).resolve()),
                    "args": (directory / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")}
        except FileNotFoundError:
            if not directory.exists():
                return None
            raise ActivationError("process_identity_unverifiable") from None
        except (OSError, IndexError, UnicodeError):
            raise ActivationError("process_identity_unverifiable") from None

    def owned(self, identity):
        if not identity:
            return False
        args = identity["args"]
        next_title = len(args) == 1 and re.fullmatch(r"next-server(?: \(v?[0-9][A-Za-z0-9.+-]*\))?", args[0])
        return bool(identity["uid"] == os.getuid() and identity["cwd"] == str(self.live.resolve())
                    and identity["exe"] == str(NODE.resolve()) and (args == self.command or next_title))

    def listeners(self):
        found = set()
        for name in ("tcp", "tcp6"):
            try:
                rows = (self.proc / "net" / name).read_text().splitlines()[1:]
            except FileNotFoundError:
                if name == "tcp6":
                    continue
                raise ActivationError("listener_inspection_unavailable") from None
            for row in rows:
                fields = row.split()
                if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == PORT:
                    found.add(fields[9])
        return found

    def owns_sockets(self, pid, inodes):
        try:
            sockets = set()
            for path in (self.proc / str(pid) / "fd").iterdir():
                try:
                    target = os.readlink(path)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    sockets.add(target[8:-1])
            return bool(inodes) and inodes.issubset(sockets)
        except OSError:
            return False

    def current(self, *, allow_starting=False):
        listeners = self.listeners()
        if not listeners:
            if allow_starting and self.state_path.is_file():
                state = self.state()
                identity = self.identity(state.get("pid", -1))
                if self.owned(identity) and identity["start_ticks"] == state.get("start_ticks"):
                    return identity
            return None
        matches = []
        for directory in self.proc.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if directory.stat().st_uid != os.getuid():
                    continue
                identity = self.identity(int(directory.name))
                if self.owned(identity) and self.owns_sockets(identity["pid"], listeners):
                    matches.append(identity)
            except (ActivationError, FileNotFoundError):
                continue
        if len(matches) != 1:
            raise ActivationError("port_owned_by_unknown_process")
        return matches[0]

    def state(self):
        if self.state_path.is_symlink():
            raise ActivationError("state_symlink_refused")
        try:
            value = json.loads(self.state_path.read_text())
            if value.get("release") != build.RELEASE:
                raise ValueError()
            return value
        except (OSError, ValueError, AttributeError):
            raise ActivationError("activation_state_missing_or_invalid") from None

    def save(self, state):
        temporary = self.state_path.with_name(self.state_path.name + f".tmp-{os.getpid()}")
        build.private_json(temporary, state)
        os.replace(temporary, self.state_path)

    def build_id(self, directory):
        try:
            value = (directory / "BUILD_ID").read_text().strip()
            if not value or len(value) > 256:
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise ActivationError("production_build_id_missing") from None

    def validate_pid_paths(self):
        for path in (self.pid_path, self.compat_pid):
            if path.is_symlink() or (path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_uid != os.getuid())):
                raise ActivationError("pid_path_unsafe")

    def terminate(self, identity):
        again = self.identity(identity["pid"])
        if not self.owned(again) or again["start_ticks"] != identity["start_ticks"]:
            raise ActivationError("process_identity_changed")
        os.kill(identity["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            again = self.identity(identity["pid"])
            if again is None:
                if self.listeners():
                    raise ActivationError("port_reoccupied_during_stop")
                return
            if again["start_ticks"] != identity["start_ticks"]:
                raise ActivationError("pid_reused_during_stop")
            time.sleep(.1)
        raise ActivationError("graceful_stop_timed_out")

    def start(self, state):
        if self.listeners():
            raise ActivationError("port_not_free_before_start")
        self.validate_pid_paths()
        environment = {key: os.environ[key] for key in ("HOME", "LANG", "LC_ALL", "TZ", "TMPDIR") if key in os.environ}
        environment.update(PATH="/opt/node-v22.23.4/bin:/usr/local/bin:/usr/bin:/bin",
                           NODE_ENV="production", LOCAL_BACKEND_OVERRIDE="1",
                           BACKEND_API_URL="http://127.0.0.1:8010", INTERNAL_API_ORIGIN="http://127.0.0.1:8010",
                           NEXT_TELEMETRY_DISABLED="1")
        descriptor = os.open(self.runtime / "logs/frontend.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "ab") as log:
            process = subprocess.Popen(self.command, cwd=self.live, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        identity = self.identity(process.pid)
        if process.poll() is not None or not self.owned(identity):
            raise ActivationError("frontend_exited_immediately")
        state.update(pid=process.pid, start_ticks=identity["start_ticks"])
        self.save(state)
        for path in (self.pid_path, self.compat_pid):
            temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(str(process.pid) + "\n")
            os.replace(temporary, path)
        return process.pid

    def page_copy(self, source):
        destination = self.live / build.PAGE
        temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)

    def restore(self, state, *, partial=False):
        # Never clobber unrelated edits or a new deployment during rollback.
        allowed_pages = {state["after_page_sha256"]}
        if partial:
            allowed_pages.add(state["before_page_sha256"])
        live_next = self.live / ".next"
        if build.sha(self.live / build.PAGE) not in allowed_pages or live_next.is_symlink() or (
            live_next.exists() and self.build_id(live_next) != state["new_build_id"]
        ) or (not partial and not live_next.exists()):
            raise ActivationError("rollback_live_inputs_changed")
        rejected = self.archive / "candidate.next"
        if rejected.exists():
            raise ActivationError("rollback_archive_already_exists")
        if self.build_id(self.archive / ".next") != state["old_build_id"] or build.sha(self.archive / "page.tsx") != state["before_page_sha256"]:
            raise ActivationError("rollback_archive_changed")
        if not partial:
            self.builder.check_fingerprint()
        identity = self.current(allow_starting=True)
        if identity is None and type(state.get("pid")) is int:
            candidate = self.identity(state["pid"])
            if candidate is not None:
                if not self.owned(candidate) or candidate["start_ticks"] != state.get("start_ticks"):
                    raise ActivationError("rollback_process_changed")
                identity = candidate
        if identity:
            self.terminate(identity)
        if not partial:
            try:
                self.builder.check_fingerprint()
                if build.sha(self.live / build.PAGE) != state["after_page_sha256"] or self.build_id(live_next) != state["new_build_id"]:
                    raise ActivationError("rollback_inputs_changed_during_stop")
            except Exception:
                # Keep every concurrent source/env/page edit. Nothing was moved.
                if live_next.exists() and self.build_id(live_next) == state["new_build_id"]:
                    self.start(state)
                raise ActivationError("rollback_aborted_source_changed_current_build_restarted") from None
        if live_next.exists():
            os.rename(live_next, rejected)
        os.rename(self.archive / ".next", self.live / ".next")
        self.page_copy(self.archive / "page.tsx")
        state["phase"] = "rolled_back"
        self.save(state)
        return self.start(state)

    def activate(self):
        with self.lock():
            self.builder.check_fingerprint()
            manifest = self.builder._manifest()
            result = json.loads(self.builder.result_path.read_text())
            candidate_next = self.builder.frontend / ".next"
            candidate_page = self.builder.frontend / build.PAGE
            if candidate_next.is_symlink() or (self.live / ".next").is_symlink():
                raise ActivationError("build_directory_symlink_refused")
            if result.get("ok") is not True or result.get("code") != "frontend_build_passed" or result.get("build_id") != self.build_id(candidate_next):
                raise ActivationError("verified_build_required")
            if build.sha(candidate_page) != manifest["candidate_page_sha256"]:
                raise ActivationError("candidate_page_changed")
            self.validate_pid_paths()
            identity = self.current()
            if identity is None:
                raise ActivationError("current_frontend_not_listening")
            old_build_id = self.build_id(self.live / ".next")
            if self.archive.exists():
                raise ActivationError("activation_archive_already_exists")
            for path in (self.root / "ops/archive", self.root / "ops/archive" / build.RELEASE):
                if path.is_symlink():
                    raise ActivationError("archive_symlink_refused")
                path.mkdir(mode=0o700, exist_ok=True)
            self.archive.mkdir(mode=0o700)
            shutil.copy2(self.live / build.PAGE, self.archive / "page.tsx")
            state = {"release": build.RELEASE, "phase": "verified", "before_page_sha256": build.sha(self.live / build.PAGE),
                     "after_page_sha256": manifest["candidate_page_sha256"], "old_build_id": old_build_id,
                     "new_build_id": result["build_id"]}
            self.save(state)
            self.terminate(identity)
            try:
                self.builder.check_fingerprint()
                if self.build_id(self.live / ".next") != old_build_id or build.sha(self.live / build.PAGE) != state["before_page_sha256"]:
                    raise ActivationError("activation_inputs_changed_during_stop")
            except Exception:
                # The original build remains in place; restart it without
                # copying the archived page over any newly edited source/env.
                if self.build_id(self.live / ".next") == old_build_id:
                    state["phase"] = "activation_aborted"
                    self.save(state)
                    self.start(state)
                raise ActivationError("activation_aborted_source_changed_original_build_restarted") from None
            try:
                os.rename(self.live / ".next", self.archive / ".next")
                os.rename(candidate_next, self.live / ".next")
                if build.sha(self.live / build.PAGE) != state["before_page_sha256"]:
                    raise ActivationError("page_changed_during_promotion")
                self.page_copy(candidate_page)
                state["phase"] = "activated"
                self.save(state)
                pid = self.start(state)
            except Exception:
                if not (self.archive / ".next").exists() and self.build_id(self.live / ".next") == old_build_id and build.sha(self.live / build.PAGE) == state["before_page_sha256"]:
                    state["phase"] = "rolled_back"
                    self.save(state)
                    self.start(state)
                else:
                    self.restore(state, partial=True)
                raise ActivationError("activation_failed_automatically_rolled_back") from None
            return {"ok": True, "code": "frontend_start_requested", "pid": pid, "build_id": state["new_build_id"],
                    "http_verified": False, "archive": str(self.archive)}

    def rollback(self):
        with self.lock():
            state = self.state()
            if state.get("phase") != "activated":
                raise ActivationError("rollback_not_applicable")
            pid = self.restore(state)
            return {"ok": True, "code": "rollback_start_requested", "pid": pid, "http_verified": False}

    def status(self):
        identity = self.current(allow_starting=True)
        return {"ok": True, "code": "frontend_managed_identity" if identity else "not_running", "pid": identity["pid"] if identity else None,
                "build_id": self.build_id(self.live / ".next"), "http_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "activate", "rollback"))
    args = parser.parse_args(argv)
    try:
        result = getattr(Manager(), args.action)()
    except (ActivationError, build.BuildError) as error:
        result = {"ok": False, "code": str(error)}
    except Exception:
        result = {"ok": False, "code": "frontend_activation_operation_failed"}
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
