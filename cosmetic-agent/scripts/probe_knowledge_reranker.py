#!/usr/bin/env python3
"""Bounded, read-only reranker preflight; optional synthetic inference only.

No model download, service startup, environment mutation, credentials, or GPU
inspection occurs here. A declared CUDA/ROCm device is not observed GPU usage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_MODEL = "bge-reranker-v2-m3"
RETRYABLE_HTTP = {429, 502, 503, 504}
SYNTHETIC_QUERY = "哪一段在討論保養時的保濕？"
SYNTHETIC_DOCUMENTS = [
    "這段文字討論汽車輪胎更換、引擎機油與公路交通。",
    "這段文字討論日常保養中的保濕，以及洗臉後的肌膚乾燥問題。",
]


class ProbeFailure(Exception):
    """Safe machine-readable failure; never contain upstream messages/URLs."""

    def __init__(self, code: str, *, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _model_compatible(actual: Any, expected: str) -> bool:
    """Allow the service's full repository id and its client's short alias."""
    if not isinstance(actual, str) or not actual.strip():
        return False
    return actual.strip().rstrip("/").casefold().split("/")[-1] == (
        expected.strip().rstrip("/").casefold().split("/")[-1]
    )


def _validate_base_url(base_url: str) -> tuple[str, str]:
    try:
        parts = urllib.parse.urlsplit(base_url)
        # Accessing .port also rejects malformed/out-of-range port values.
        _ = parts.port
    except (ValueError, TypeError):
        raise ProbeFailure("invalid_base_url") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in base_url)
    ):
        raise ProbeFailure("invalid_base_url")
    base = base_url.rstrip("/")
    if not parts.path.rstrip("/").endswith("/v1"):
        raise ProbeFailure("base_url_requires_v1")
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return base, origin


def _safe_device(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    # Do not emit arbitrary upstream strings, paths, or credential-bearing URLs.
    if normalized in {"cpu", "cuda", "rocm", "hip", "mps"}:
        return normalized
    prefix, separator, ordinal = normalized.partition(":")
    if separator and prefix in {"cuda", "rocm", "hip", "mps"} and ordinal.isdigit():
        return f"{prefix}:{int(ordinal)}"
    return "unrecognized"


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        get_retries: int,
        opener: Callable[..., Any] | None = None,
    ):
        self.timeout = timeout
        self.get_retries = get_retries
        self.opener = opener or urllib.request.urlopen

    def request(self, method: str, url: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method=method,
        )
        # POST inference is never retried automatically, even after a timeout.
        attempts = 1 + self.get_retries if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ProbeFailure("response_too_large")
                return json.loads(raw)
            except urllib.error.HTTPError as error:
                if error.code in RETRYABLE_HTTP and attempt + 1 < attempts:
                    continue
                raise ProbeFailure("http_error", http_status=error.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt + 1 < attempts:
                    continue
                raise ProbeFailure("transport_error") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ProbeFailure("invalid_json") from None
        raise ProbeFailure("transport_error")  # Defensive, attempts is always >= 1.


def run_probe(
    base_url: str,
    *,
    expected_model: str = DEFAULT_MODEL,
    exercise: bool = False,
    timeout: float = 2.5,
    get_retries: int = 1,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Probe live readiness and optional ranking; returns only safe JSON data."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "code": "not_started",
        "checks": {},
        "gpu": {
            "declared_device": None,
            "declared_gpu": None,
            "observed_gpu": None,
            "verification": "not_observed_by_http_probe",
        },
        "limits": {
            "timeout_seconds": None,
            "get_attempts_max": None,
            "post_attempts_max": 1 if exercise else 0,
        },
    }
    stage = "configuration"
    try:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 5
            or isinstance(get_retries, bool)
            or not isinstance(get_retries, int)
            or not 0 <= get_retries <= 2
        ):
            raise ProbeFailure("invalid_limits")
        if (
            not isinstance(expected_model, str)
            or not expected_model.strip()
            or len(expected_model) > 256
            or "://" in expected_model
        ):
            raise ProbeFailure("invalid_expected_model")
        base, origin = _validate_base_url(base_url)
        result["limits"].update(
            timeout_seconds=timeout, get_attempts_max=1 + get_retries
        )
        client = HttpClient(timeout=timeout, get_retries=get_retries, opener=opener)
        stage = "readiness"
        ready = client.request("GET", origin + "/readyz")
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            raise ProbeFailure("service_not_ready")
        if not _model_compatible(ready.get("model"), expected_model):
            raise ProbeFailure("readiness_model_mismatch")
        device = _safe_device(ready.get("device"))
        result["gpu"].update(
            declared_device=device,
            declared_gpu=(
                device.split(":")[0] in {"cuda", "rocm", "hip"}
                if device not in {None, "unrecognized"}
                else None
            ),
        )
        result["checks"][stage] = {"ok": True, "model_compatible": True}

        stage = "models"
        models = client.request("GET", base + "/models")
        items = models.get("data") if isinstance(models, dict) else None
        if not isinstance(items, list):
            raise ProbeFailure("invalid_models_response")
        compatible = [
            item["id"]
            for item in items
            if isinstance(item, dict) and _model_compatible(item.get("id"), expected_model)
        ]
        if not compatible:
            raise ProbeFailure("model_not_advertised")
        actual_model = compatible[0]
        result["checks"][stage] = {"ok": True, "model_compatible": True}

        stage = "rerank"
        if exercise:
            response = client.request(
                "POST",
                base + "/rerank",
                {
                    "model": actual_model,
                    "query": SYNTHETIC_QUERY,
                    "documents": SYNTHETIC_DOCUMENTS,
                    "top_n": len(SYNTHETIC_DOCUMENTS),
                    "return_documents": False,
                },
            )
            if not isinstance(response, dict):
                raise ProbeFailure("invalid_rerank_response")
            if not _model_compatible(response.get("model"), expected_model):
                raise ProbeFailure("rerank_model_mismatch")
            entries = response.get("results")
            if not isinstance(entries, list) or len(entries) != 2:
                raise ProbeFailure("invalid_rerank_results")
            scores: dict[int, float] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ProbeFailure("invalid_rerank_result")
                index, score = entry.get("index"), entry.get("relevance_score")
                if type(index) is not int or index not in {0, 1} or index in scores:
                    raise ProbeFailure("invalid_rerank_index")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise ProbeFailure("invalid_rerank_score")
                scores[index] = float(score)
            if scores[1] <= scores[0]:
                raise ProbeFailure("synthetic_relevance_order_failed")
            if entries[0]["index"] != 1:
                raise ProbeFailure("invalid_rerank_order")
            result["checks"][stage] = {
                "ok": True,
                "valid_indexes": True,
                "finite_scores": True,
                "valid_result_order": True,
                "relevant_outranks_distractor": True,
                "evidence_quality_verified": False,
            }
        else:
            result["checks"][stage] = {"skipped": True, "reason": "exercise_not_requested"}
        result.update(ok=True, code="preflight_passed" if exercise else "health_passed")
    except ProbeFailure as failure:
        result["code"] = failure.code
        result["failed_stage"] = stage
        result["checks"][stage] = {"ok": False, "code": failure.code}
        if failure.http_status is not None:
            result["checks"][stage]["http_status"] = failure.http_status
    except Exception:
        # Do not leak exception strings: urllib errors can include credential URLs.
        result.update(code="unexpected_probe_error", failed_stage=stage)
        result["checks"][stage] = {"ok": False, "code": "unexpected_probe_error"}
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("RERANKER_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("RERANKER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--exercise", action="store_true", help="Run one harmless synthetic ranking")
    parser.add_argument("--timeout", type=float, default=2.5)
    parser.add_argument("--get-retries", type=int, default=1)
    args = parser.parse_args(argv)
    result = run_probe(
        args.base_url,
        expected_model=args.model,
        exercise=args.exercise,
        timeout=args.timeout,
        get_retries=args.get_retries,
    )
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
