"""Reusable client for OpenAI-compatible chat completion services."""

from __future__ import annotations

import os
from typing import Any, Mapping, Protocol, Sequence

import requests
from requests.adapters import HTTPAdapter


class _Response(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> Any: ...


class _Session(Protocol):
    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _Response: ...


class ChatCompletionError(RuntimeError):
    """The configured model endpoint did not return a usable completion."""


class ChatCompletionClient:
    """Synchronous OpenAI-compatible client with bounded connection pooling.

    POST retries are disabled deliberately.  Retrying model generation inside
    this low-level client can multiply GPU work during overload; retry or
    fallback policy belongs to the calling service.
    """

    def __init__(
        self,
        *,
        base_url: str,
        default_timeout_seconds: float = 30.0,
        pool_maxsize: int = 8,
        session: _Session | None = None,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        if not normalized_url:
            raise ValueError("chat completion base_url must not be empty")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")

        self.base_url = normalized_url
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.pool_maxsize = max(1, int(pool_maxsize))
        self._session = session or self._build_session()

    @classmethod
    def from_env(
        cls,
        *,
        base_url_env: str = "TAIDE_BASE_URL",
        default_base_url: str = "http://localhost:8000/v1",
        timeout_env: str = "MODEL_HTTP_TIMEOUT_SECONDS",
    ) -> "ChatCompletionClient":
        return cls(
            base_url=os.getenv(base_url_env, default_base_url),
            default_timeout_seconds=max(0.1, float(os.getenv(timeout_env, "30"))),
            pool_maxsize=max(1, int(os.getenv("MODEL_HTTP_POOL_MAXSIZE", "8"))),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=min(4, self.pool_maxsize),
            pool_maxsize=self.pool_maxsize,
            max_retries=0,
            pool_block=True,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> str:
        if not messages:
            raise ValueError("chat completion messages must not be empty")
        if not model.strip():
            raise ValueError("chat completion model must not be empty")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        payload: dict[str, Any] = {
            "model": model.strip(),
            "messages": [dict(message) for message in messages],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if extra_body:
            payload.update(dict(extra_body))

        try:
            response = self._session.post(
                self.endpoint,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
            choices = body.get("choices") if isinstance(body, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ValueError("chat completion response has no text content")
            return content.strip()
        except ChatCompletionError:
            raise
        except Exception as exc:
            raise ChatCompletionError(
                f"chat completion request to {self.endpoint} failed: {exc}"
            ) from exc
