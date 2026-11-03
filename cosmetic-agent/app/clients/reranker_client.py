"""Fail-fast HTTP client for the multilingual knowledge reranker.

The service contract follows the common /rerank shape used by BGE
deployments. POST retries are deliberately disabled: repeating an expensive
GPU request under load makes queueing worse and can reorder user requests.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter


class RerankerServiceError(RuntimeError):
    """Raised when the reranker is unavailable or returns an invalid result."""


class _Session(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
    ) -> Any: ...


class RerankerClient:
    """Synchronous client used from the route's worker thread."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        pool_maxsize: int = 8,
        session: _Session | None = None,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        if not normalized_url:
            raise ValueError("reranker base_url must not be empty")
        if not model.strip():
            raise ValueError("reranker model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("reranker timeout_seconds must be positive")
        self.base_url = normalized_url
        self.model = model.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.pool_maxsize = max(1, int(pool_maxsize))
        self._session = session or self._build_session()

    @classmethod
    def from_env(cls) -> "RerankerClient":
        return cls(
            base_url=os.getenv("RERANKER_BASE_URL", ""),
            model=os.getenv("RERANKER_MODEL", "bge-reranker-v2-m3"),
            timeout_seconds=max(
                0.1, float(os.getenv("RERANKER_TIMEOUT_SECONDS", "2.5"))
            ),
            pool_maxsize=max(
                1, int(os.getenv("RERANKER_HTTP_POOL_MAXSIZE", "8"))
            ),
        )

    @property
    def endpoint(self) -> str:
        return self.base_url + "/rerank"

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

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[dict[str, float | int]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise ValueError("reranker query must be non-empty")
        if not documents or any(not str(item).strip() for item in documents):
            raise ValueError("reranker documents must be non-empty strings")
        safe_top_n = max(1, min(int(top_n), len(documents)))
        try:
            response = self._session.post(
                self.endpoint,
                json={
                    "model": self.model,
                    "query": clean_query,
                    "documents": documents,
                    "top_n": safe_top_n,
                    "return_documents": False,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            raw_results = (
                payload.get("results") or payload.get("data")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(raw_results, list):
                raise ValueError("reranker response has no results list")
            results: list[dict[str, float | int]] = []
            seen: set[int] = set()
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                index = item.get("index")
                score = item.get("relevance_score", item.get("score"))
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(documents)
                    or index in seen
                    or not isinstance(score, (int, float))
                    or isinstance(score, bool)
                ):
                    continue
                results.append({"index": index, "score": float(score)})
                seen.add(index)
                if len(results) >= safe_top_n:
                    break
            if not results:
                raise ValueError("reranker response contains no valid result")
            return results
        except RerankerServiceError:
            raise
        except Exception as exc:
            raise RerankerServiceError(
                f"reranker request to {self.endpoint} failed: {exc}"
            ) from exc
