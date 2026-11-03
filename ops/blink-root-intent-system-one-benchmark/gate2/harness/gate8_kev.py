from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request


class KevLifecycleError(RuntimeError):
    pass


def _start_ticks(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().split()[21]


def _port_free(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


class KevService:
    def __init__(self, project_root: Path, model_pin: Path, private: Path, public: Path, port: int = 8230):
        self.project_root = Path(project_root)
        self.model_pin = json.loads(Path(model_pin).read_text(encoding="utf-8"))
        self.private = Path(private)
        self.public = Path(public)
        self.port = int(port)
        self.proc = None
        self.ticks = None
        self.log_handle = None

    def start(self):
        if not _port_free(self.port):
            raise KevLifecycleError(f"owned Kev port {self.port} already in use")
        py = self.project_root / ".runtime/kev-py312/bin/python"
        adapter = Path(self.model_pin["adapter_dir"])
        if not py.is_file() or not adapter.is_dir():
            raise KevLifecycleError("Gate7 pinned Kev runtime/model missing")
        self.private.mkdir(parents=True, exist_ok=True)
        self.public.mkdir(parents=True, exist_ok=True)
        log = self.private / "kev-service.log"
        self.log_handle = log.open("wb")
        env = dict(os.environ)
        env.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            KEV_DTYPE="bf16",
            KEV_FUSED="0",
            KEV_CUDA_GRAPHS="0",
            TOKENIZERS_PARALLELISM="false",
            HF_HOME=str(self.project_root / ".cache/huggingface"),
            XDG_CACHE_HOME=str(self.project_root / ".cache/xdg"),
            TMPDIR=str(self.project_root / ".cache/tmp"),
        )
        Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        cmd = [str(py), "-m", "kev.serve", "--run", str(adapter), "--host", "127.0.0.1", "--port", str(self.port)]
        self.proc = subprocess.Popen(cmd, stdout=self.log_handle, stderr=subprocess.STDOUT, env=env, cwd=self.project_root)
        self.ticks = _start_ticks(self.proc.pid)
        (self.public / "process.json").write_text(
            json.dumps({"pid": self.proc.pid, "start_ticks": self.ticks, "port": self.port}, indent=2) + "\n"
        )
        deadline = time.time() + 240
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise KevLifecycleError("Kev exited before readiness")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models", timeout=2) as r:
                    body = json.loads(r.read().decode())
                    if r.status == 200:
                        (self.public / "models.json").write_text(json.dumps(body, indent=2) + "\n")
                        return
            except Exception as exc:
                last = type(exc).__name__
            time.sleep(1)
        raise KevLifecycleError("Kev readiness timeout:" + str(last))

    def stop(self):
        result = {
            "pid": getattr(self.proc, "pid", None),
            "same_process": False,
            "signal_sent": False,
            "kill_sent": False,
            "stopped": False,
            "port_released": False,
            "returncode": None,
        }
        try:
            if self.proc is None:
                result["stopped"] = True
                result["port_released"] = _port_free(self.port)
                return result

            pid = self.proc.pid
            proc_path = Path(f"/proc/{pid}/stat")
            # Popen.poll()/wait() reaps an exited child. Do that before using
            # `/proc` existence as liveness evidence so a zombie is not
            # misclassified as a still-running Kev server.
            rc = self.proc.poll()
            if rc is not None:
                result["returncode"] = rc
                result["stopped"] = True
                result["port_released"] = _port_free(self.port)
                return result

            if not proc_path.exists():
                rc = self.proc.wait(timeout=2)
                result["returncode"] = rc
                result["stopped"] = True
                result["port_released"] = _port_free(self.port)
                return result

            if proc_path.read_text().split()[21] != self.ticks:
                raise KevLifecycleError("Kev PID identity changed")
            result["same_process"] = True

            os.kill(pid, signal.SIGTERM)
            result["signal_sent"] = True
            try:
                rc = self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Re-validate identity immediately before SIGKILL. If the
                # process exited between checks, poll/wait will reap it instead.
                rc = self.proc.poll()
                if rc is None:
                    if not proc_path.exists() or proc_path.read_text().split()[21] != self.ticks:
                        raise KevLifecycleError("Kev identity unavailable before SIGKILL")
                    os.kill(pid, signal.SIGKILL)
                    result["kill_sent"] = True
                    rc = self.proc.wait(timeout=10)

            result["returncode"] = rc
            result["stopped"] = self.proc.poll() is not None
            # The owned server is not considered cleaned up if another child
            # retained the listening socket.
            deadline = time.time() + 5
            while time.time() < deadline and not _port_free(self.port):
                time.sleep(0.1)
            result["port_released"] = _port_free(self.port)
            return result
        finally:
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self.public.mkdir(parents=True, exist_ok=True)
            (self.public / "cleanup.json").write_text(json.dumps(result, indent=2) + "\n")

