"""Mac-only status and complete-checkpoint backup; never signal research/serving."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[3]
DEFAULT_RUN = "/mnt/goodagents/outputs/skin-vlm/qwen-app-cycle4-20261116-r2"
DEFAULT_LOCAL = REPO / ".runtime/reports/qwen-app-cycle4-20261116"
HOST = "root@140.119.24.206"
CONTROL = "/tmp/blink-mi300x-control"
MANIFEST = "files.sha256.json"
TERMINAL = {"complete", "failed", "suspended_for_service_health", "pilot_complete"}
REPORT_ARTIFACTS = {"protocol.json", "progress.json", "selection.json"}


def terminal_status(value):
    return value in TERMINAL or isinstance(value, str) and (value == "suspended" or value.startswith("suspended_"))


class IntegrityError(ValueError):
    pass


class ConnectionUnavailable(RuntimeError):
    pass


class RetryDownload(RuntimeError):
    pass


def sftp_quote(value):
    text = str(value)
    if not text or any(ord(char) < 32 for char in text):
        raise IntegrityError("Unsafe SFTP batch pathname")
    return '"' + ''.join('\\' + char if char in '\\"*?[]' else char for char in text) + '"'


def relative(value):
    if (not isinstance(value, str) or not value or value.startswith("/")
            or any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                   for part in value.split("/"))):
        raise IntegrityError("Unsafe relative checkpoint path")
    return PurePosixPath(value)


def run_paths(value):
    path = PurePosixPath(value)
    if (str(path.parent) != "/mnt/goodagents/outputs/skin-vlm"
            or not re.fullmatch(r"qwen-app-cycle4-[0-9]{8}-r[0-9]+", path.name)
            or ".." in value.split("/")):
        raise IntegrityError("Only an explicitly named cycle4 research run is allowed")
    return path, PurePosixPath("/mnt/goodagents/checkpoints/skin-appearance-decision") / path.name


def checkpoint_relative(value, root):
    try:
        result = PurePosixPath(value).relative_to(root)
    except ValueError as error:
        raise IntegrityError("Checkpoint escapes this run") from error
    relative(str(result))
    if len(result.parts) != 2 or not re.fullmatch(r"step-[0-9]{6,}", result.name):
        raise IntegrityError("Only completed trial step directories are allowed")
    return str(result)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def no_links(path):
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise IntegrityError("Symlink backup paths are forbidden")


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def atomic_bytes(path, value):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".partial")
    no_links(temporary)
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


# Root/run values come only from run_paths(). No environment, tokens, images,
# worker logs, or checkpoint tensor contents are read by this remote program.
REMOTE = r'''
from pathlib import Path
import json,hashlib,os,re,stat
request=REQUEST
raw_reports={}
def rel(value):
    if not isinstance(value,str) or not value or value.startswith('/') or any(p in {'','.','..'} or not re.fullmatch(r'[A-Za-z0-9_.-]+',p) for p in value.split('/')):raise ValueError('unsafe_relative')
    return value
def clean(path):
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('symlink_forbidden')
def read(path):
    clean(path)
    if path.stat().st_size>5*1024*1024:raise ValueError('oversized_metadata')
    raw=path.read_bytes();raw_reports[path.name]=raw
    return json.loads(raw)
try:
    if request['operation']=='state':
        root=Path(request['report']);clean(root);out={};records={}
        for n in ['job.json','status.json','progress.json','selection.json','pilot.json','protocol.json']:
            p=root/n
            if p.is_file():records[n]=read(p)
        job=records.get('job.json',{});pid=job.get('pid');alive=False;process_state=None
        if type(pid) is int:
            p=Path('/proc')/str(pid)/'stat'
            try:
                fields=p.read_text().rsplit(')',1)[1].split();process_state=fields[0]
                alive=fields[0] not in {'Z','X'} and fields[19]==str(job.get('start_ticks'))
            except FileNotFoundError:pass
        out['job']={k:job[k] for k in ['pid','start_ticks','trainer_sha256','runtime_ready_sha256'] if k in job};out['pid_alive']=alive;out['pid_state']=process_state
        s=records.get('status.json',{});out['status']={k:s[k] for k in ['status','candidate','completed','total','fingerprint','error_type'] if k in s}
        targets=[]
        pilot=records.get('pilot.json',{})
        if pilot.get('checkpoint'):targets.append({'kind':'pilot','checkpoint':pilot['checkpoint']})
        progress=records.get('progress.json',{});selection=records.get('selection.json',{})
        for row in progress.get('results',[])+selection.get('candidates',[]):
            if row.get('adapter'):targets.append({'kind':'trial_final','checkpoint':row['adapter']})
        out['targets']=targets;out['completed_trials']=progress.get('completed',len(selection.get('candidates',[])));out['selection_present']=bool(selection)
        out['artifacts']={n:{'content':raw_reports[n].decode('utf-8'),'sha256':hashlib.sha256(raw_reports[n]).hexdigest()} for n in ['protocol.json','progress.json','selection.json'] if n in raw_reports}
        print(json.dumps({'ok':True,'value':out}))
    else:
        root=Path(request['checkpoints']);folder=root/rel(request['relative']);clean(folder)
        manifest=folder/'files.sha256.json';clean(manifest);raw=manifest.read_bytes()
        if len(raw)>2*1024*1024:raise ValueError('oversized_manifest')
        value=json.loads(raw);files=value.get('files')
        if value.get('schema_version')!=1 or not isinstance(files,dict) or not 1<=len(files)<=256:raise ValueError('invalid_manifest')
        actual=set();sizes={}
        for p in folder.rglob('*'):
            clean(p)
            if p.is_dir():continue
            if not stat.S_ISREG(p.stat().st_mode):raise ValueError('nonregular_checkpoint_file')
            name=rel(p.relative_to(folder).as_posix());actual.add(name);sizes[name]=p.stat().st_size
        if actual!=set(files)|{'files.sha256.json'}:raise ValueError('checkpoint_file_set_changed')
        for name,h in files.items():
            rel(name)
            if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):raise ValueError('invalid_sha256')
        if not {'state.pt','state.json','metadata.json','python-random-state.json'}<=set(files) or not any(n.endswith('.safetensors') for n in files):raise ValueError('incomplete_training_state')
        files=dict(files);files['files.sha256.json']=hashlib.sha256(raw).hexdigest()
        print(json.dumps({'ok':True,'value':{'files':files,'sizes':sizes,'manifest_sha256':files['files.sha256.json'],'symlinks':False}}))
except FileNotFoundError:
    print(json.dumps({'ok':False,'retry':True,'error':'metadata_not_available'}))
except Exception as error:
    print(json.dumps({'ok':False,'retry':False,'error':type(error).__name__}))
'''


class Remote:
    def __init__(self, run, bandwidth_kbps=4096):
        self.report, self.checkpoints = run_paths(run)
        if type(bandwidth_kbps) is not int or not 1 <= bandwidth_kbps <= 1000000:
            raise ValueError("Bandwidth must be an integer kbit/s limit between1 and1000000")
        self.bandwidth_kbps = bandwidth_kbps

    def request(self, operation, **values):
        request = dict(operation=operation, report=str(self.report), checkpoints=str(self.checkpoints), **values)
        script = REMOTE.replace("REQUEST", repr(request), 1)
        args = ["ssh", "-S", CONTROL, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=15", "-p", "30892", HOST, "python3", "-"]
        try:
            result = subprocess.run(args, input=script, text=True, capture_output=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConnectionUnavailable(type(error).__name__) from error
        if result.returncode:
            raise ConnectionUnavailable("ssh_unavailable")
        try:
            message = json.loads(result.stdout)
        except ValueError as error:
            raise ConnectionUnavailable("invalid_remote_reply") from error
        if not message.get("ok"):
            if message.get("retry"):
                raise ConnectionUnavailable(message.get("error"))
            raise IntegrityError(message.get("error"))
        return message["value"]

    def state(self):
        return self.request("state")

    def describe(self, checkpoint):
        return self.request("checkpoint", relative=str(relative(checkpoint)))

    def download(self, checkpoint, member, target):
        relative(checkpoint);relative(member)
        source = self.checkpoints / checkpoint / member
        no_links(target)
        if target.exists() and not target.is_file():
            raise IntegrityError("Partial transfer must be a regular file")
        options = ["-o", "BatchMode=yes", "-o", "ControlPath=" + CONTROL,
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-P", "30892"]
        batch = None
        if target.exists() and target.stat().st_size:
            args = ["sftp", "-q", "-b", "-", "-R", "8", "-B", "16384", "-l", str(self.bandwidth_kbps), *options, HOST]
            batch = ("@reget " + sftp_quote(source) + " " + sftp_quote(target) + "\nbye\n").encode()
        else:
            args = ["scp", "-q", "-l", str(self.bandwidth_kbps), "-X", "nrequests=8", "-X", "buffer=16384",
                    *options, HOST + ":" + str(source), str(target)]
        try:
            child = subprocess.Popen(args, stdin=subprocess.PIPE if batch else subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            if batch:
                child.stdin.write(batch)
                child.stdin.close()
            deadline = time.monotonic() + 1800
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise ConnectionUnavailable("transfer_timeout")
                try:
                    code = child.wait(timeout=min(60, remaining))
                    break
                except subprocess.TimeoutExpired:
                    callback = getattr(self, "heartbeat", None)
                    if callback:
                        try:
                            callback()
                        except Exception:
                            child.terminate()
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait()
                            raise
        except OSError as error:
            raise ConnectionUnavailable(type(error).__name__) from error
        if code:
            raise ConnectionUnavailable("scp_unavailable")


def validate_descriptor(value):
    if value.get("symlinks") is not False or not isinstance(value.get("files"), dict):
        raise IntegrityError("Unverified remote checkpoint descriptor")
    files = value["files"]
    if not 2 <= len(files) <= 257 or files.get(MANIFEST) != value.get("manifest_sha256"):
        raise IntegrityError("Invalid checkpoint manifest identity")
    for name, expected in files.items():
        relative(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise IntegrityError("Invalid checkpoint SHA")
    return files


class Watcher:
    def __init__(self, remote, local):
        self.remote = remote
        self.local = Path(local).absolute()
        no_links(self.local)
        self.local.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_name = remote.report.name
        self.record = dict(run=str(remote.report), watcher_pid=os.getpid(), watcher_state="starting",
                           training_status=None, connection="unknown", checkpoints={}, errors=0,
                           bandwidth_kbps=getattr(remote, "bandwidth_kbps", 4096))
        self.remote.heartbeat = self.heartbeat
        self.verified_complete = set()
        previous = self.local / "runs" / self.run_name / "watch-status.json"
        if previous.exists():
            no_links(previous)
            saved = json.loads(previous.read_text())
            if saved.get("run") != str(remote.report):
                raise IntegrityError("Local run receipt differs")
            self.record["checkpoints"] = saved.get("checkpoints", {})

    def save(self):
        self.record["updated_at"] = time.time()
        cp = self.record["checkpoints"].values()
        self.record["counts"] = {"complete_checkpoints": sum(x.get("complete") is True for x in cp),
                                 "verified_files": sum(len(x.get("files", {})) for x in self.record["checkpoints"].values())}
        atomic_json(self.local / "runs" / self.run_name / "watch-status.json", self.record)
        atomic_json(self.local / "watch-status.json", self.record)

    def backup(self, checkpoint):
        checkpoint_relative(str(self.remote.checkpoints / checkpoint), self.remote.checkpoints)
        descriptor = self.remote.describe(checkpoint)
        files = validate_descriptor(descriptor)
        base = self.local / "mac-checkpoints" / self.run_name
        destination = base / checkpoint
        partial = destination.with_name(destination.name + ".partial")
        no_links(destination);no_links(partial)
        record = self.record["checkpoints"].setdefault(checkpoint, {})
        if record.get("manifest_sha256", descriptor["manifest_sha256"]) != descriptor["manifest_sha256"]:
            raise IntegrityError("Previously seen checkpoint manifest changed")
        if checkpoint in self.verified_complete and destination.is_dir():
            return
        record.update(manifest_sha256=descriptor["manifest_sha256"], total_files=len(files), local_path=str(destination),
                      sizes=descriptor.get("sizes", {}))
        record["complete"] = False
        folder = destination if destination.exists() else partial
        no_links(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        record["files"] = {}
        for name, expected in files.items():
            target = folder / name
            no_links(target)
            if target.exists() and (not target.is_file() or digest(target) != expected):
                if folder == destination:
                    raise IntegrityError("Completed local backup changed")
            if not target.exists() or digest(target) != expected:
                if folder == destination:
                    raise IntegrityError("Completed local backup is incomplete")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = target.with_name(target.name + ".downloading")
                no_links(temporary)
                self.record["watcher_state"] = "copying_checkpoint"
                self.record["copying"] = {"checkpoint": checkpoint, "file": name}
                self.save()
                expected_size = descriptor.get("sizes", {}).get(name)
                if temporary.exists() and type(expected_size) is int and temporary.stat().st_size > expected_size:
                    self.reject_partial(temporary, checkpoint, name, expected, "oversized_partial")
                self.remote.download(checkpoint, name, temporary)
                no_links(temporary)
                if not temporary.is_file() or digest(temporary) != expected:
                    if temporary.is_file():
                        self.reject_partial(temporary, checkpoint, name, expected, "full_sha_mismatch")
                    raise RetryDownload("Downloaded checkpoint SHA mismatch; fresh retry required")
                temporary.chmod(0o600)
                temporary.replace(target)
            record["files"][name] = expected
            self.save()
        actual = set()
        for target in folder.rglob("*"):
            no_links(target)
            if target.is_file():
                actual.add(target.relative_to(folder).as_posix())
        if actual != set(files):
            raise IntegrityError("Unexpected local checkpoint files")
        if folder == partial:
            no_links(destination)
            if destination.exists():
                raise IntegrityError("Backup destination was concurrently occupied")
            partial.rename(destination)
        record["complete"] = True
        self.verified_complete.add(checkpoint)
        self.record.pop("copying", None)
        self.save()

    def reject_partial(self, path, checkpoint, name, expected, reason):
        no_links(path)
        destination = self.local / "rejected-partials" / self.run_name / checkpoint / (name + "." + str(time.time_ns()))
        no_links(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        item = {"file": name, "bytes": path.stat().st_size, "actual_sha256": digest(path),
                "expected_sha256": expected, "reason": reason, "preserved_path": str(destination)}
        path.rename(destination)
        self.record["checkpoints"][checkpoint].setdefault("rejected_partials", []).append(item)
        self.save()

    def refresh(self):
        state = self.remote.state()
        artifacts = state.get("artifacts", {})
        if not isinstance(artifacts, dict) or not set(artifacts) <= REPORT_ARTIFACTS:
            raise IntegrityError("Unexpected remote report artifact")
        receipts = self.record.setdefault("remote_report_artifacts", {})
        for name, artifact in artifacts.items():
            if not isinstance(artifact, dict) or not isinstance(artifact.get("content"), str):
                raise IntegrityError("Invalid report artifact")
            raw = artifact["content"].encode("utf-8")
            if len(raw) > 5 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
                raise IntegrityError("Remote report artifact SHA mismatch")
            try:
                decoded = json.loads(raw)
            except ValueError as error:
                raise IntegrityError("Invalid report artifact JSON") from error
            if not isinstance(decoded, dict):
                raise IntegrityError("Report artifact must be a JSON object")
            path = self.local / "remote-reports" / name
            no_links(path)
            if not path.exists() or digest(path) != artifact["sha256"]:
                atomic_bytes(path, raw)
                atomic_bytes(self.local / "runs" / self.run_name / "remote-reports" / name, raw)
            receipts[name] = {"sha256": artifact["sha256"], "local_path": str(path)}
        self.record.update(connection="connected", remote_pid=state.get("job", {}).get("pid"),
                           remote_pid_alive=state.get("pid_alive"), training_status=state.get("status", {}).get("status"),
                           remote_pid_state=state.get("pid_state"), remote_polled_at=time.time(),
                           remote_status=state.get("status", {}), completed_trials=state.get("completed_trials", 0))
        self.record.pop("last_error_type", None)
        self.latest_state = state
        self.save()
        return state

    def heartbeat(self):
        try:
            self.refresh()
        except ConnectionUnavailable:
            self.record.update(connection="unavailable", last_error_type="ConnectionUnavailable",
                               errors=self.record["errors"] + 1)
            self.save()

    def poll(self):
        state = self.refresh()
        self.record["watcher_state"] = "watching" if state.get("pid_alive") else "worker_not_running_status_preserved"
        handled = set()
        while True:
            targets = {checkpoint_relative(row["checkpoint"], self.remote.checkpoints) for row in self.latest_state.get("targets", [])}
            for checkpoint in sorted(targets - handled):
                self.backup(checkpoint)
                handled.add(checkpoint)
            # A long rate-limited transfer may outlast a trial. Refresh before
            # terminal exit so newly completed final checkpoints are not lost.
            state = self.refresh()
            latest = {checkpoint_relative(row["checkpoint"], self.remote.checkpoints) for row in state.get("targets", [])}
            if latest <= handled:
                break
        terminal = terminal_status(self.record["training_status"])
        if terminal:
            self.record["terminal_training_status"] = self.record["training_status"]
        self.record["watcher_state"] = "terminal_backups_complete" if terminal else (
            "watching" if state.get("pid_alive") else "worker_not_running_status_preserved")
        self.save()
        return terminal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-run", default=DEFAULT_RUN)
    parser.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--bandwidth-kbps", type=int, default=4096)
    args = parser.parse_args()
    remote = Remote(args.remote_run, bandwidth_kbps=args.bandwidth_kbps)
    watcher = Watcher(remote, args.local_root)
    lock = watcher.local / (remote.report.name + ".watch.lock")
    no_links(lock)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Watcher for this run is already active")
        while True:
            start = time.monotonic()
            try:
                terminal = watcher.poll()
            except (ConnectionUnavailable, RetryDownload) as error:
                watcher.record.update(connection="unavailable" if isinstance(error, ConnectionUnavailable) else "connected",
                                      watcher_state="retrying_connection" if isinstance(error, ConnectionUnavailable) else "retrying_download",
                                      last_error_type=type(error).__name__, errors=watcher.record["errors"] + 1)
                watcher.save()
                terminal = False
                if args.once:
                    return 1
            except IntegrityError as error:
                watcher.record.update(watcher_state="backup_integrity_error", last_error_type=type(error).__name__)
                watcher.save()
                return 1
            except OSError as error:
                watcher.record.update(watcher_state="local_backup_error", last_error_type=type(error).__name__)
                watcher.save()
                return 1
            print(json.dumps({"status": watcher.record["training_status"], "pid_alive": watcher.record.get("remote_pid_alive"),
                              "watcher": watcher.record["watcher_state"], "counts": watcher.record.get("counts")}), flush=True)
            if args.once or terminal:
                return 0
            time.sleep(max(0, 60 - (time.monotonic() - start)))


if __name__ == "__main__":
    raise SystemExit(main())
