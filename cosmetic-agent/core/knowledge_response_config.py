"""Configuration boundary for the Knowledge response model."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class KnowledgeResponseConfig:
    """Resolved Knowledge response settings with legacy-compatible defaults."""

    provider: str
    model: str
    max_output_tokens: int
    timeout_seconds: float
    max_retries: int
    reasoning_effort: str
    taide_api_base: str
    taide_api_key: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "KnowledgeResponseConfig":
        values = os.environ if environ is None else environ
        provider = "terra"
        max_output_tokens = int(
            values.get("KNOWLEDGE_RESPONSE_MAX_OUTPUT_TOKENS", "384")
        )

        if provider == "terra":
            return cls(
                provider=provider,
                model=(
                    values.get("KNOWLEDGE_RESPONSE_MODEL", "gpt-5.6-terra").strip()
                    or "gpt-5.6-terra"
                ),
                max_output_tokens=max_output_tokens,
                timeout_seconds=float(
                    values.get("TERRA_RESPONSE_TIMEOUT_SECONDS", "45")
                ),
                max_retries=int(values.get("TERRA_RESPONSE_MAX_RETRIES", "1")),
                reasoning_effort=values.get(
                    "TERRA_RESPONSE_REASONING_EFFORT",
                    "none",
                ),
                taide_api_base="",
                taide_api_key="EMPTY",
            )

        return cls(
            provider=provider,
            model=(
                values.get("KNOWLEDGE_RESPONSE_MODEL", "gpt-5.6-terra").strip()
                or "gpt-5.6-terra"
            ),
            max_output_tokens=max_output_tokens,
            timeout_seconds=float(
                values.get("TAIDE_RESPONSE_TIMEOUT_SECONDS", "60")
            ),
            max_retries=0,
            reasoning_effort="none",
            taide_api_base=values.get(
                "TAIDE_BASE_URL",
                "http://localhost:8000/v1",
            ).strip("/"),
            taide_api_key=values.get("TAIDE_API_KEY", "EMPTY"),
        )
