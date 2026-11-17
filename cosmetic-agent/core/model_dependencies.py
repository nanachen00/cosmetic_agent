"""Readiness dependencies for the deployed Terra / private GPU split."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping


def model_readiness_checks(
    check_http: Callable[[str], bool],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Callable[[], bool]]:
    values = os.environ if environ is None else environ
    # This is a configuration check, not a paid inference/availability probe.
    checks = {
        "terra_config": lambda: bool(values.get("OPENAI_API_KEY", "").strip()),
        "embedding": lambda: check_http(
            values.get("EMBEDDING_HEALTH_URL", "http://127.0.0.1:8002/healthz")
        ),
    }
    if values.get("ENABLE_TAIDE", "0").strip() == "1":
        checks.update({
            "taide": lambda: check_http(
                values.get("TAIDE_HEALTH_URL", "http://127.0.0.1:8000/healthz")
            ),
            "taide_router": lambda: check_http(
                values.get("TAIDE_ROUTER_HEALTH_URL", "http://127.0.0.1:8001/healthz")
            ),
        })
    return checks
