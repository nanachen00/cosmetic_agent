"""Read-only reranker readiness, not overall retrieval or measured GPU health.

The readiness endpoint must not run inference. The service performs its own
load/warm-up checks before setting ready=true. Results (including failures) are
cached for two seconds; a failed refresh never reuses expired healthy state.
"""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_KNOWN_MODEL_ALIASES = frozenset({"BAAI/bge-reranker-v2-m3", "bge-reranker-v2-m3"})
_MAX_BODY_BYTES = 65536


def _result(code: str, *, ready: bool = False, model_verified: bool = False,
            accelerator_reported: bool = False) -> dict:
    # Never include URLs, response bodies, exception text, or credentials.
    return {"ready": ready, "code": code, "model_verified": model_verified,
            "accelerator_reported": accelerator_reported}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _configuration(base_url, expected_model, timeout_seconds):
    if not isinstance(base_url, str) or not base_url.strip():
        return None, _result("reranker_not_configured")
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in base_url):
            raise ValueError
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") not in {"", "/v1"}
                or "%" in parsed.hostname or "\\" in parsed.netloc):
            raise ValueError
        parsed.port  # Validate malformed/non-numeric/out-of-range ports.
        if (not isinstance(expected_model, str) or not expected_model.strip()
                or len(expected_model) > 256):
            raise ValueError
        if isinstance(timeout_seconds, bool):
            raise ValueError
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= 2:
            raise ValueError
        model = expected_model.strip()
        origin = urlunsplit((parsed.scheme, parsed.netloc.lower(), "", "", ""))
        return (origin, model, timeout), None
    except (TypeError, ValueError):
        return None, _result("reranker_invalid_configuration")


class RerankerReadinessCache:
    def __init__(self, *, opener: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._opener = opener or build_opener(_NoRedirect()).open
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}
        self._inflight = {}

    def check(self, base_url, expected_model, timeout_seconds=0.5) -> dict:
        configuration, error = _configuration(base_url, expected_model, timeout_seconds)
        if error is not None:
            return error
        origin, model, timeout = configuration
        model_key = "BAAI/bge-reranker-v2-m3" if model in _KNOWN_MODEL_ALIASES else model
        key = origin, model_key
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and self._clock() < cached[0]:
                return dict(cached[1])
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                if len(self._inflight) >= 8:
                    return _result("reranker_health_busy")
                event = threading.Event()
                self._inflight[key] = event
        if not leader:
            # Do not wait indefinitely behind a slow/unresponsive upstream.
            if event.wait(timeout):
                with self._lock:
                    cached = self._entries.get(key)
                    if cached is not None and self._clock() < cached[0]:
                        return dict(cached[1])
            return _result("reranker_health_busy")
        result = _result("reranker_health_unavailable")
        try:
            result = self._probe(origin, model, timeout)
        except Exception:
            # Also guard injected openers; never return upstream error strings.
            pass
        finally:
            # An unexpected BaseException must not leave other requests blocked.
            with self._lock:
                if len(self._entries) >= 128:
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = self._clock() + 2.0, result
                self._inflight.pop(key, None)
                event.set()
        return dict(result)

    def _probe(self, origin: str, expected_model: str, timeout: float) -> dict:
        request = Request(origin + "/readyz", headers={"Accept": "application/json"}, method="GET")
        with self._opener(request, timeout=timeout) as response:
            if response.getcode() != 200:
                return _result("reranker_health_unavailable")
            body = response.read(_MAX_BODY_BYTES + 1)
        if len(body) > _MAX_BODY_BYTES:
            return _result("reranker_health_invalid_response")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, UnicodeError):
            return _result("reranker_health_invalid_response")
        if not isinstance(payload, dict):
            return _result("reranker_health_invalid_response")
        if payload.get("ready") is not True:
            return _result("reranker_not_ready")
        actual_model = payload.get("model")
        same_model = actual_model == expected_model or (
            actual_model in _KNOWN_MODEL_ALIASES and expected_model in _KNOWN_MODEL_ALIASES
        ) if isinstance(actual_model, str) else False
        if not same_model:
            return _result("reranker_model_mismatch")
        # This is upstream metadata presence, NOT a claim that GPU usage was measured.
        accelerator = payload.get("accelerator")
        return _result("reranker_ready", ready=True, model_verified=True,
                       accelerator_reported=isinstance(accelerator, dict) and bool(accelerator))


_DEFAULT_CACHE = RerankerReadinessCache()


def check_reranker_ready(base_url, expected_model, timeout_seconds=0.5) -> dict:
    return _DEFAULT_CACHE.check(base_url, expected_model, timeout_seconds)
