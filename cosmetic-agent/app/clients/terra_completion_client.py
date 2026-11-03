"""Shared Terra text-completion adapter for existing agents."""

from __future__ import annotations

import os
from collections.abc import Sequence
from threading import Lock
from typing import Any

from openai import OpenAI


_CLIENT_LOCK = Lock()
_SHARED_CLIENT: OpenAI | None = None


def _shared_client() -> OpenAI:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        with _CLIENT_LOCK:
            if _SHARED_CLIENT is None:
                _SHARED_CLIENT = OpenAI(
                    api_key=os.getenv("OPENAI_API_KEY"),
                    timeout=float(os.getenv("TERRA_TIMEOUT_SECONDS", "20")),
                    max_retries=int(os.getenv("TERRA_MAX_RETRIES", "0")),
                )
    return _SHARED_CLIENT


class TerraCompletionClient:
    """Expose a small complete API backed by OpenAI Responses."""

    DEFAULT_MODEL = "gpt-5.6-terra"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.model = model or os.getenv("TERRA_MODEL", self.DEFAULT_MODEL)
        self.client = (
            OpenAI(
                api_key=api_key,
                timeout=float(os.getenv("TERRA_TIMEOUT_SECONDS", "20")),
                max_retries=int(os.getenv("TERRA_MAX_RETRIES", "0")),
            )
            if api_key
            else _shared_client()
        )

    @classmethod
    def from_env(cls) -> "TerraCompletionClient":
        return cls()

    def complete(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 600,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        # Keep temperature for call-site compatibility; Responses does not need it.
        del temperature
        client = self.client
        if timeout_seconds is not None:
            client = client.with_options(timeout=timeout_seconds, max_retries=0)
        response = client.responses.create(
            model=model or self.model,
            input=list(messages),
            reasoning={"effort": os.getenv("TERRA_REASONING_EFFORT", "none")},
            text={"verbosity": "low"},
            max_output_tokens=max_tokens,
            store=False,
        )
        text = str(getattr(response, "output_text", "") or "").strip()
        if not text:
            raise RuntimeError("Terra returned an empty response")
        return text
