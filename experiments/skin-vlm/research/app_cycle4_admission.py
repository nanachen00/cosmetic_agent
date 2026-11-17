"""Cooperative research yielding to the production photo service.

This is a polling admission signal, not GPU isolation or a latency guarantee.
Only the background thread performs HTTP; generation checks a local Event.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time
import urllib.request


class PhotoAdmissionTimeout(TimeoutError):
    """Production did not become stably idle within the permitted wait."""


class PhotoAdmission:
    URL = "http://127.0.0.1:8025/health"
    GRACE_SECONDS = 0.15
    MAX_BODY_BYTES = 4096

    def __init__(self, log_path=None, *, poll_interval=0.15, timeout=2.0, opener=None):
        if not math.isfinite(poll_interval) or not 0 < poll_interval <= 0.15:
            raise ValueError("poll_interval must be in (0, 0.15]")
        if not math.isfinite(timeout) or not 0 < timeout <= 2.0:
            raise ValueError("timeout must be in (0, 2]")
        self.log_path = Path(log_path) if log_path is not None else None
        self.poll_interval = poll_interval
        self.timeout = timeout
        # Loopback health must not be sent through an inherited HTTP proxy.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        self.busy_event = threading.Event()
        self.busy_event.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._closed = False
        self._healthy_streak = 0
        self._idle_since = None
        self._last_state = None
        self._total_wait_seconds = 0.0

    def _log_locked(self, state):
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"timestamp": time.time(), "state": state,
                      "total_wait_seconds": round(self._total_wait_seconds, 6)}
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _record_poll(self, healthy):
        with self._lock:
            if self._closed:
                return
            self._healthy_streak = self._healthy_streak + 1 if healthy else 0
            busy = self._healthy_streak < 2
            if busy:
                self.busy_event.set()
                self._idle_since = None
            elif self.busy_event.is_set():
                self._idle_since = time.monotonic()
                self.busy_event.clear()
            state = "busy" if busy else "idle"
            if state != self._last_state:
                self._log_locked(state)
                self._last_state = state

    def _poll_once(self):
        healthy = False
        try:
            with self._opener(self.URL, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                body = response.read(self.MAX_BODY_BYTES + 1)
                if status == 200 and len(body) <= self.MAX_BODY_BYTES:
                    value = json.loads(body)
                    healthy = (isinstance(value, dict) and value.get("ready") is True
                               and value.get("busy") is False and value.get("failed", False) is False)
        except Exception:
            # Do not retain response bodies, exception strings, or image information.
            healthy = False
        self._record_poll(healthy)

    def _run(self):
        try:
            while not self._stop.wait(self.poll_interval):
                self._poll_once()
        except Exception:
            # A broken logger or monitor must never leave a stale idle signal.
            with self._lock:
                self.busy_event.set()
                self._idle_since = None
            self._stop.set()

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("PhotoAdmission is closed")
            if self._thread is not None:
                return self
        # A failed first probe blocks research before the background thread starts.
        self._poll_once()
        with self._lock:
            if self._closed:
                raise RuntimeError("PhotoAdmission is closed")
            self._thread = threading.Thread(target=self._run, name="photo-admission", daemon=True)
            self._thread.start()
        return self

    def wait_idle(self, check_callback=None, max_wait=300):
        if not math.isfinite(max_wait) or max_wait < 0:
            raise ValueError("max_wait must be finite and nonnegative")
        started = time.monotonic()
        deadline = started + max_wait
        try:
            while True:
                if check_callback is not None:
                    check_callback()
                now = time.monotonic()
                with self._lock:
                    stopped = self._closed or self._stop.is_set()
                    idle_since = self._idle_since
                    busy = self.busy_event.is_set()
                if stopped:
                    raise PhotoAdmissionTimeout("Photo admission monitor is stopped")
                # Already stable idle is a no-sleep fast path between research kernels.
                if not busy and idle_since is not None and now >= idle_since + self.GRACE_SECONDS:
                    # Recheck the Event after the callback and grace interval.
                    if not self.busy_event.is_set():
                        return
                if now >= deadline:
                    raise PhotoAdmissionTimeout("Photo service did not become stably idle")
                delay = min(self.poll_interval, deadline - now)
                if not busy and idle_since is not None:
                    delay = min(delay, max(0.0, idle_since + self.GRACE_SECONDS - now))
                self._stop.wait(delay)
        finally:
            with self._lock:
                self._total_wait_seconds += time.monotonic() - started

    def make_stopping_criterion(self):
        # Training-only dependencies stay optional when importing CPU admission code.
        import torch
        from transformers import StoppingCriteria

        busy_event = self.busy_event

        class PhotoPriorityStoppingCriteria(StoppingCriteria):
            def __init__(self):
                self.triggered = False

            def __call__(self, input_ids, scores, **kwargs):
                self.triggered = self.triggered or busy_event.is_set()
                return torch.full((input_ids.shape[0],), self.triggered,
                                  dtype=torch.bool, device=input_ids.device)

        return PhotoPriorityStoppingCriteria()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.busy_event.set()
            self._idle_since = None
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout + self.poll_interval + 0.1)
        with self._lock:
            self._log_locked("closed")

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
