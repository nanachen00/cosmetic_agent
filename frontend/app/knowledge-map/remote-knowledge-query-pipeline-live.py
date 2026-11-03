"""Isolated retrieval pipeline for the experimental knowledge-map API.

This module does not participate in the production chat/KnowledgeSubAgent path.
It resolves a graph center with deterministic aliases first, then BGE-M3
semantic retrieval and a multilingual reranker, and finally enforces a fixed
relation policy before any graph is returned or explained.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from app.clients.embedding_client import EmbeddingClient
from app.clients.reranker_client import RerankerClient
from app.repositories.knowledge_seed_repository import normalize_lookup
from app.services.knowledge_explorer_service import KnowledgeExplorerService
from app.services.knowledge_graph_view import apply_graph_view
from app.services.knowledge_retrieval_health import check_reranker_ready
from app.services.knowledge_async_work import run_shielded_worker
from core.admission import (
    AdmissionFull,
    AdmissionTimeout,
    ChatAdmissionController,
    RedisChatAdmissionController,
)


ALLOWED_KNOWLEDGE_RELATIONS = frozenset({
    "ACTIVE_IN",
    "ALIAS_OF",
    "APPLIES_TO_RULE",
    "BELONGS_TO_CATEGORY",
    "BELONGS_TO_DRUG_CLASS",
    "CONFLICTS_WITH",
    "CONTAINS",
    "HAS_APPROVED_INDICATION",
    "HAS_FUNCTION",
    "HAS_SYMPTOM",
    "HAS_TRIGGER",
    "MANUFACTURED_BY",
    "MAY_INCREASE_IRRITATION_WITH",
    "MAY_REFER_TO",
    "MENTIONS_CONCERN",
    "NAVIGATES_TO_CONCERN",
    "NAVIGATES_TO_INGREDIENT",
    "REGULATES_CATEGORY",
    "RELATED_CONCERN",
    "RELEVANT_TO_CARE",
    "REQUIRES_CAUTION",
    "REQUIRES_FUNCTION",
    "SUPPORTS_NEED",
    "SUITABLE_FOR_CONCERN",
    "USE_WITH_CAUTION_FOR",
    "WARNS_CONCERN",
})
logger = logging.getLogger(__name__)


class KnowledgeQueryUnavailable(RuntimeError):
    """Raised when a required semantic stage is not configured or available."""


class _Embedder(Protocol):
    model: str

    def embed_query(self, query: str) -> list[float]: ...


class _Reranker(Protocol):
    model: str

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[dict[str, float | int]]: ...


def _admission_from_env(max_running: int, max_queued: int):
    redis_url = (
        os.getenv("KNOWLEDGE_QUERY_ADMISSION_REDIS_URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
    )
    require_redis = (
        os.getenv("KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS", "0") == "1"
    )
    if redis_url:
        try:
            return RedisChatAdmissionController(
                redis_url,
                max_running=max_running,
                max_queued=max_queued,
                key_prefix=os.getenv(
                    "KNOWLEDGE_QUERY_ADMISSION_KEY_PREFIX",
                    "blink:knowledge:admission",
                ),
                lease_seconds=float(
                    os.getenv("KNOWLEDGE_QUERY_ADMISSION_LEASE_SECONDS", "30")
                ),
                poll_seconds=float(
                    os.getenv("KNOWLEDGE_QUERY_ADMISSION_POLL_SECONDS", "0.05")
                ),
            )
        except (ImportError, ModuleNotFoundError) as exc:
            if require_redis:
                raise RuntimeError(
                    "Redis asyncio support is required for knowledge admission"
                ) from exc
    if require_redis:
        raise RuntimeError(
            "KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS=1 but no Redis URL is configured"
        )
    return ChatAdmissionController(
        max_running=max_running,
        max_queued=max_queued,
    )


def query_pipeline_enabled() -> bool:
    return os.getenv("KNOWLEDGE_QUERY_PIPELINE_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class KnowledgeQueryPipeline:
    """Alias -> BGE-M3 -> reranker -> trusted graph -> Terra boundary."""

    def __init__(
        self,
        explorer: KnowledgeExplorerService,
        *,
        embedding_client: _Embedder,
        reranker_client: _Reranker | None,
        semantic_limit: int = 12,
        rerank_limit: int = 5,
        minimum_semantic_score: float = 0.45,
        max_running: int = 2,
        max_queued: int = 8,
        queue_timeout_seconds: float = 0.5,
        admission: Any | None = None,
    ) -> None:
        self.explorer = explorer
        self.embedding_client = embedding_client
        if getattr(explorer, "cloud_only", False):
            explorer.repository.configure_semantic(embedding_client)
        self.reranker_client = reranker_client
        self.semantic_limit = max(1, min(int(semantic_limit), 30))
        self.rerank_limit = max(1, min(int(rerank_limit), self.semantic_limit))
        self.minimum_semantic_score = float(minimum_semantic_score)
        self.admission = admission or ChatAdmissionController(
            max_running=max_running,
            max_queued=max_queued,
        )
        self.queue_timeout_seconds = max(0.01, float(queue_timeout_seconds))
        self._worker_tasks = set()

    @classmethod
    def from_env(
        cls,
        explorer: KnowledgeExplorerService,
    ) -> "KnowledgeQueryPipeline":
        reranker_url = os.getenv("RERANKER_BASE_URL", "").strip()
        reranker = RerankerClient.from_env() if reranker_url else None
        max_running = int(os.getenv("KNOWLEDGE_QUERY_MAX_RUNNING", "2"))
        max_queued = int(os.getenv("KNOWLEDGE_QUERY_MAX_QUEUED", "8"))
        return cls(
            explorer,
            embedding_client=EmbeddingClient.from_env(),
            reranker_client=reranker,
            semantic_limit=int(os.getenv("KNOWLEDGE_SEMANTIC_CANDIDATES", "12")),
            rerank_limit=int(os.getenv("KNOWLEDGE_RERANK_TOP_N", "5")),
            minimum_semantic_score=float(
                os.getenv("KNOWLEDGE_MIN_SEMANTIC_SCORE", "0.45")
            ),
            max_running=max_running,
            max_queued=max_queued,
            admission=_admission_from_env(max_running, max_queued),
            queue_timeout_seconds=float(
                os.getenv("KNOWLEDGE_QUERY_QUEUE_TIMEOUT_SECONDS", "0.5")
            ),
        )

    def status(self) -> dict[str, Any]:
        health = (check_reranker_ready(self.reranker_client.base_url, self.reranker_client.model)
                  if self.reranker_client else {"ready": False, "code": "reranker_not_configured"})
        return {
            "enabled": query_pipeline_enabled(),
            "ready": bool(health["ready"]),
            "reranker_health": health,
            "readiness_scope": "reranker-only-not-vector-or-end-to-end",
            "embedding_model": self.embedding_client.model,
            "reranker_model": (
                self.reranker_client.model if self.reranker_client else None
            ),
            "accelerator_target": "mi300x-external-services",
            "relation_policy": "fixed-allowlist",
            "allowed_relation_count": len(ALLOWED_KNOWLEDGE_RELATIONS),
            "admission": {
                "max_running": self.admission.max_running,
                "max_queued": self.admission.max_queued,
                "active": self.admission.active,
                "queued": self.admission.queued,
                "queue_timeout_seconds": self.queue_timeout_seconds,
                "backend": (
                    "redis"
                    if isinstance(self.admission, RedisChatAdmissionController)
                    else "process-local"
                ),
            },
        }

    @staticmethod
    def _is_exact(query_key: str, node: dict[str, Any]) -> bool:
        matched = normalize_lookup(str(node.get("matched_alias") or ""))
        if matched and matched == query_key:
            return True
        if normalize_lookup(str(node.get("name") or "")) == query_key:
            return True
        properties = dict(node.get("properties") or {})
        aliases = properties.get("aliases") or []
        return any(normalize_lookup(str(alias)) == query_key for alias in aliases)

    def _exact_candidates(self, query: str) -> list[dict[str, Any]]:
        query_key = normalize_lookup(query)
        if not query_key:
            return []
        curated_search = getattr(
            self.explorer.repository, "curated_search", None
        )
        if callable(curated_search):
            try:
                curated_nodes = curated_search(query, limit=30)
            except Exception:
                logger.warning("curated exact lookup unavailable", exc_info=True)
                if getattr(self.explorer, "cloud_only", False) or os.getenv(
                    "KNOWLEDGE_CURATED_REQUIRED", "0"
                ).strip().lower() in {"1", "true", "yes", "on"}:
                    raise
                curated_nodes = []
        else:
            # Legacy and injected repositories may not expose the optional
            # curated overlay API. Continue with seed/live exact lookup.
            curated_nodes = []
        exact = [node for node in curated_nodes if self._is_exact(query_key, node)]
        if exact:
            return exact
        seed_nodes = self.explorer.seed_repository.search(query, limit=30)
        exact = [node for node in seed_nodes if self._is_exact(query_key, node)]
        if exact:
            return exact
        try:
            live_nodes = self.explorer.repository.search(query, limit=30)
        except Exception:
            return []
        return [node for node in live_nodes if self._is_exact(query_key, node)]

    @staticmethod
    def _document(node: dict[str, Any]) -> str:
        properties = dict(node.get("properties") or {})
        aliases = properties.get("aliases") or []
        parts = [
            str(node.get("type") or "Knowledge"),
            str(node.get("name") or ""),
            str(node.get("description") or ""),
            " ".join(str(alias) for alias in aliases[:12]),
        ]
        return " | ".join(part.strip() for part in parts if part.strip())[:2_000]

    def _canonicalize_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Map a live Neo4j/embedding hit back to the curated graph node ID.

        Semantic search may return Neo4j element IDs from an older or
        independently indexed graph. The knowledge map must expand the curated
        graph, whose stable IDs are used by relations and explanations.
        """
        candidate_name = str(candidate.get("name") or "").strip()
        query_key = normalize_lookup(candidate_name)
        if not query_key:
            return candidate
        try:
            seed_nodes = self.explorer.seed_repository.search(candidate_name, limit=12)
        except Exception:
            return candidate
        for seed_node in seed_nodes:
            if not self._is_exact(query_key, seed_node):
                continue
            resolved = dict(candidate)
            resolved["semantic_source_id"] = str(candidate.get("id") or "")
            resolved["id"] = str(seed_node.get("id") or candidate.get("id") or "")
            resolved["name"] = str(seed_node.get("name") or candidate_name)
            resolved["type"] = str(seed_node.get("type") or candidate.get("type") or "Knowledge")
            properties = dict(seed_node.get("properties") or {})
            if properties:
                resolved["properties"] = properties
            return resolved
        return candidate

    def _semantic_candidates(self, query: str) -> list[dict[str, Any]]:
        if getattr(self.explorer, "source_mode", None) == "json":
            return self.explorer.search(query, limit=self.semantic_limit)
        if self.reranker_client is None:
            raise KnowledgeQueryUnavailable("reranker_not_configured")
        try:
            vector = self.embedding_client.embed_query(query)
        except Exception as exc:
            raise KnowledgeQueryUnavailable("embedding_unavailable") from exc
        try:
            candidates = self.explorer.repository.semantic_search(
                vector,
                limit=self.semantic_limit,
            )
        except Exception as exc:
            raise KnowledgeQueryUnavailable("graph_search_unavailable") from exc
        candidates = [
            node
            for node in candidates
            if float(node.get("semantic_score") or -1)
            >= self.minimum_semantic_score
        ]
        if not candidates:
            return []
        try:
            ranked = self.reranker_client.rerank(
                query,
                [self._document(node) for node in candidates],
                top_n=min(self.rerank_limit, len(candidates)),
            )
        except Exception as exc:
            raise KnowledgeQueryUnavailable("reranker_unavailable") from exc
        results: list[dict[str, Any]] = []
        for item in ranked:
            node = dict(candidates[int(item["index"])])
            node["rerank_score"] = round(float(item["score"]), 6)
            node = self._canonicalize_candidate(node)
            results.append(node)
        return results

    @staticmethod
    def _filter_graph(graph: dict[str, Any]) -> dict[str, Any]:
        raw_edges = list(graph.get("edges") or [])
        result = apply_graph_view(
            graph,
            allowed_relations=ALLOWED_KNOWLEDGE_RELATIONS,
        )
        edges = list(result.get("edges") or [])
        node_ids = {str(graph["center"]["id"])}
        for edge in edges:
            node_ids.add(str(edge.get("source") or ""))
            node_ids.add(str(edge.get("target") or ""))
        source_ids: set[str] = set()
        references: set[str] = set()
        statuses: set[str] = set()
        for edge in edges:
            statuses.add(str(edge.get("evidence_status") or "unknown"))
            for source_id in edge.get("source_ids") or []:
                if str(source_id).strip():
                    source_ids.add(str(source_id).strip())
            reference = str(edge.get("evidence_reference") or "").strip()
            if reference:
                references.add(reference)
        result["evidence"] = {
            "edge_ids": [str(edge.get("id")) for edge in edges if edge.get("id")],
            "statuses": sorted(statuses),
            "source_ids": sorted(source_ids),
            "references": sorted(references),
        }
        result["relation_policy"] = {
            "mode": "typed-view-profile",
            "allowed_relations": sorted(ALLOWED_KNOWLEDGE_RELATIONS),
            "visible_relations": list(result.get("view", {}).get("visible_relations") or []),
            "rejected_edge_count": len(raw_edges) - len(edges),
        }
        return result

    def resolve(
        self,
        query: str,
        *,
        depth: int = 1,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        clean_query = " ".join(str(query or "").split())[:160]
        if not clean_query:
            raise ValueError("knowledge query must not be empty")
        candidates = self._exact_candidates(clean_query)
        strategy = "exact-alias"
        if not candidates:
            candidates = self._semantic_candidates(clean_query)
            strategy = "bge-m3-plus-reranker"
        if not candidates:
            return None
        center_id = str(candidates[0]["id"])
        try:
            graph = self.explorer.neighborhood(
                center_id, depth=depth, limit=limit
            )
        except Exception as exc:
            raise KnowledgeQueryUnavailable("graph_lookup_unavailable") from exc
        if graph is None:
            return None
        result = self._filter_graph(graph)
        result["resolution"] = {
            "query": clean_query,
            "canonical_node_id": center_id,
            "matched_alias": candidates[0].get("matched_alias"),
            "strategy": strategy,
            "ambiguous": len(candidates) > 1,
            "candidates": [
                {
                    "node_id": str(node.get("id")),
                    "name": str(node.get("name") or ""),
                    "type": str(node.get("type") or "Knowledge"),
                    "semantic_score": node.get("semantic_score"),
                    "rerank_score": node.get("rerank_score"),
                }
                for node in candidates[: self.rerank_limit]
            ],
        }
        return result

    async def resolve_async(
        self,
        query: str,
        *,
        depth: int = 1,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        """Run blocking model/Neo4j I/O off-loop behind bounded FIFO admission."""
        ticket = None
        worker_started = False
        try:
            ticket = await self.admission.reserve()
            await ticket.wait(self.queue_timeout_seconds)
            worker_started = True
            return await run_shielded_worker(
                self.resolve, query,
                on_complete=ticket.release,
                registry=self._worker_tasks,
                depth=depth, limit=limit,
            )
        except KnowledgeQueryUnavailable:
            raise
        except (AdmissionFull, AdmissionTimeout) as exc:
            raise KnowledgeQueryUnavailable("knowledge_query_busy") from exc
        except Exception as exc:
            raise KnowledgeQueryUnavailable(
                "knowledge_query_backend_unavailable"
            ) from exc
        finally:
            if ticket is not None and not worker_started:
                await ticket.release()

    async def explain(self, graph: dict[str, Any]) -> dict[str, Any]:
        """Use the existing Terra explainer, then enforce citation containment."""
        result = await self.explorer.explain(str(graph["center"]["id"]))
        if result is None:
            raise KnowledgeQueryUnavailable("explanation_node_not_found")
        allowed_edge_ids = {
            str(edge.get("id"))
            for edge in graph.get("edges") or []
            if edge.get("id")
        }
        cited_edge_ids = {
            str(edge_id)
            for edge_id in (result.get("citations") or {}).get("edge_ids") or []
        }
        if not cited_edge_ids.issubset(allowed_edge_ids):
            return self.explorer._fallback(
                graph,
                reason="relation_policy_violation",
            )
        return result
