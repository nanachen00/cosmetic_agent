"""HTTP client for the dedicated BGE-M3 embedding service.

This module owns endpoint construction, connection pooling, timeouts and
response validation.  Callers should not issue ad-hoc requests to port 8002.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter


class _Response(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> Any: ...


class _Session(Protocol):
    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _Response: ...


class EmbeddingServiceError(RuntimeError):
    """The dedicated embedding service could not return a valid vector."""


class EmbeddingClient:
    """Small synchronous client for the OpenAI-compatible embedding endpoint.

    A long-lived ``requests.Session`` reuses local TCP connections.  Automatic
    POST retries are intentionally disabled so overload fails quickly instead
    of multiplying work on the single GPU.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        dimension: int = 1024,
        pool_maxsize: int = 16,
        session: _Session | None = None,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        if not normalized_url:
            raise ValueError("embedding base_url must not be empty")
        if not model.strip():
            raise ValueError("embedding model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout_seconds must be positive")
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")

        self.base_url = normalized_url
        self.model = model.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.dimension = int(dimension)
        self.pool_maxsize = max(1, int(pool_maxsize))
        self._session = session or self._build_session()

    @classmethod
    def from_env(cls) -> "EmbeddingClient":
        return cls(
            base_url=os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8002/v1"),
            model=os.getenv("EMBEDDING_MODEL", "bge-m3"),
            timeout_seconds=max(
                0.1, float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "2.0"))
            ),
            dimension=max(1, int(os.getenv("EMBEDDING_DIMENSION", "1024"))),
            pool_maxsize=max(
                1, int(os.getenv("EMBEDDING_HTTP_POOL_MAXSIZE", "16"))
            ),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/embeddings"

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

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("embedding query must be a non-empty string")

        try:
            response = self._session.post(
                self.endpoint,
                json={"model": self.model, "input": [query]},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            vector = data[0].get("embedding") if isinstance(data, list) and data else None
            if not isinstance(vector, list) or len(vector) != self.dimension:
                raise ValueError(
                    f"expected a {self.dimension}-d embedding vector"
                )
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in vector
            ):
                raise ValueError("embedding vector contains non-numeric values")
            return [float(value) for value in vector]
        except EmbeddingServiceError:
            raise
        except Exception as exc:
            raise EmbeddingServiceError(
                f"embedding request to {self.endpoint} failed: {exc}"
            ) from exc
