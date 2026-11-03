"""Isolated retrieval pipeline for the experimental knowledge-map API.

This module does not participate in the production chat/KnowledgeSubAgent path.
It resolves a graph center with deterministic aliases first, then BGE-M3
semantic retrieval and a multilingual reranker, and finally enforces a fixed
relation policy before any graph is returned or explained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Protocol

from app.clients.embedding_client import EmbeddingClient
from app.clients.reranker_client import RerankerClient
from app.clients.terra_completion_client import TerraCompletionClient
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

_QUERY_INTERPRETER_SYSTEM_PROMPT = (
    "你是 BLINK AI 的美妝知識搜尋理解器。你的工作不是診斷或回答問題，而是把使用者的自然語句、"
    "俗稱、別名或錯別字整理成可能存在於美妝知識圖譜的標準實體名稱。"
    "實體類型只可為肌膚狀況、成分、照護需求或藥品。"
    "請輸出一個 JSON 物件，格式固定為 "
    '{"terms":["最可能詞","次可能詞"],"intent":"concern|ingredient|care|drug|unknown",'
    '"mode":"single|compare","primary_term":"使用者主要想查的實體"}。'
    "terms 依可能性排序，最多四個，每個不超過四十字；primary_term 必須取自 terms。"
    "使用者明確提到的成分或藥品通常是主要查詢對象；膚質與症狀可保留為其他 terms。"
    "異維A酸、異維甲酸、isotretinoin 或口服A酸請正規化為口服 A 酸 (Isotretinoin)。"
    "只輸出 JSON，不要解釋。"
    "不要捏造療效、診斷、用量或圖譜關係；不確定時可回傳空 terms。"
)


class KnowledgeQueryNotResolved(Exception):
    """A safe, structured outcome for a query that has no verified graph node."""

    def __init__(
        self,
        *,
        code: str,
        query: str,
        terms: list[str] | None = None,
        intent: str = "unknown",
        mode: str = "single",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.query = query
        self.terms = list(terms or [])[:4]
        self.intent = intent
        self.mode = mode


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


class _QueryInterpreter(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str: ...


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
        query_interpreter: _QueryInterpreter | None = None,
        query_interpreter_model: str | None = None,
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
        self.query_interpreter = query_interpreter
        self.query_interpreter_model = (
            query_interpreter_model
            or os.getenv("KNOWLEDGE_QUERY_TERRA_MODEL", "").strip()
            or TerraCompletionClient.DEFAULT_MODEL
        )
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
        interpreter = None
        if os.getenv("KNOWLEDGE_QUERY_LLM_ENABLED", "1").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            try:
                interpreter = TerraCompletionClient.from_env()
            except Exception:
                logger.warning("knowledge query interpreter unavailable", exc_info=True)
        return cls(
            explorer,
            embedding_client=EmbeddingClient.from_env(),
            reranker_client=reranker,
            query_interpreter=interpreter,
            query_interpreter_model=(
                os.getenv("KNOWLEDGE_QUERY_TERRA_MODEL", "").strip()
                or os.getenv("KNOWLEDGE_TERRA_MODEL", "").strip()
                or TerraCompletionClient.DEFAULT_MODEL
            ),
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
            "query_understanding": {
                "enabled": self.query_interpreter is not None,
                "provider": "terra" if self.query_interpreter is not None else None,
                "model": self.query_interpreter_model if self.query_interpreter is not None else None,
                "verified_against_graph": True,
            },
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

    def _interpret_query(self, query: str) -> dict[str, Any]:
        """Extract graph candidates and UX intent without trusting LLM facts."""
        empty = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "available": False,
            "parsed": False,
        }
        if self.query_interpreter is None:
            return empty
        try:
            response = self.query_interpreter.complete(
                model=self.query_interpreter_model,
                messages=[
                    {"role": "system", "content": _QUERY_INTERPRETER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "使用者輸入：" + query + "\n"
                            "請整理最可能的圖譜實體候選詞；比較兩個主題時保留兩個候選。"
                        ),
                    },
                ],
                temperature=0.0,
                max_tokens=200,
            )
        except Exception:
            logger.warning("knowledge query interpretation failed", exc_info=True)
            return empty
        text = str(response or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {**empty, "available": True}
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {**empty, "available": True}
        if not isinstance(payload, dict):
            return {**empty, "available": True}

        raw_terms = payload.get("terms")
        result: list[str] = []
        seen: set[str] = set()
        if isinstance(raw_terms, list):
            for value in raw_terms:
                if not isinstance(value, str):
                    continue
                term = " ".join(value.split())[:40]
                key = normalize_lookup(term)
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append(term)
                if len(result) >= 4:
                    break

        intent = str(payload.get("intent") or "unknown").strip().lower()
        if intent not in {"concern", "ingredient", "care", "drug", "unknown"}:
            intent = "unknown"
        has_compare_marker = len(result) > 1 and any(
            marker in query for marker in ("比較", "差別", "一樣", "不同", "還是", "跟", "和")
        )
        mode = str(payload.get("mode") or "single").strip().lower()
        if mode not in {"single", "compare"}:
            mode = "single"
        if has_compare_marker:
            mode = "compare"
        primary_term = ""
        raw_primary = payload.get("primary_term")
        if isinstance(raw_primary, str):
            candidate = " ".join(raw_primary.split())[:40]
            candidate_key = normalize_lookup(candidate)
            if candidate_key and any(normalize_lookup(term) == candidate_key for term in result):
                primary_term = candidate
        return {
            "terms": result,
            "intent": intent,
            "mode": mode,
            "primary_term": primary_term,
            "available": True,
            "parsed": True,
        }

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
        interpretation = {
            "terms": [],
            "intent": "unknown",
            "mode": "single",
            "primary_term": "",
            "available": False,
            "parsed": False,
        }
        interpreted_terms: list[str] = []
        unresolved_terms: list[str] = []

        # These are canonical taxonomy aliases, not medical advice.  Resolve
        # them before the broader topical-A-acid concept so the safety layer can
        # handle oral isotretinoin correctly.
        normalized_input = normalize_lookup(clean_query)
        oral_isotretinoin_aliases = (
            "異維a酸",
            "異維甲酸",
            "isotretinoin",
            "口服異維a酸",
        )
        if any(alias in normalized_input for alias in oral_isotretinoin_aliases):
            candidates = self._exact_candidates("異維A酸")
            if not candidates:
                candidates = self._exact_candidates("口服 A 酸 (Isotretinoin)")
            if candidates:
                strategy = "explicit-canonical-alias"

        if not candidates:
            interpretation = self._interpret_query(clean_query)
            interpreted_terms = list(interpretation.get("terms") or [])[:4]
            intent = str(interpretation.get("intent") or "unknown")
            mode = str(interpretation.get("mode") or "single")
            primary_term = str(interpretation.get("primary_term") or "")

            # Only an unknown decision with no candidate terms is authoritative.
            if bool(interpretation.get("parsed")) and intent == "unknown" and not interpreted_terms:
                raise KnowledgeQueryNotResolved(
                    code="knowledge_query_out_of_scope",
                    query=clean_query,
                    terms=[],
                    intent=intent,
                    mode=mode,
                )

            seen_candidate_ids: set[str] = set()
            exact_hits: list[tuple[dict[str, Any], int, str]] = []
            semantic_hits: list[tuple[dict[str, Any], int, str]] = []

            # Resolve every interpreted term. A modifier in terms[0] must not
            # hide an explicit entity in terms[1]. Exact hits are collected
            # across all terms before any semantic fallback is considered.
            for term_index, term in enumerate(interpreted_terms):
                term_candidates = self._exact_candidates(term)
                if not term_candidates:
                    unresolved_terms.append(term)
                    continue
                for node in term_candidates:
                    node_id = str(node.get("id") or "")
                    if not node_id or node_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(node_id)
                    exact_hits.append((node, term_index, term))

            if mode == "compare":
                ordered: list[dict[str, Any]] = []
                ordered_ids: set[str] = set()
                for term_index, term in enumerate(interpreted_terms):
                    term_hits = [item for item in exact_hits if item[1] == term_index]
                    if not term_hits:
                        fallback = self._semantic_candidates(term)
                        if fallback:
                            term_hits = [(fallback[0], term_index, term)]
                            if term in unresolved_terms:
                                unresolved_terms.remove(term)
                    if not term_hits:
                        continue
                    node = term_hits[0][0]
                    node_id = str(node.get("id") or "")
                    if node_id and node_id not in ordered_ids:
                        ordered_ids.add(node_id)
                        ordered.append(node)
                candidates = ordered
                strategy = "terra-query-understanding" if exact_hits else "terra-query-understanding-plus-bge-m3"
            else:
                if not exact_hits:
                    for term_index, term in enumerate(interpreted_terms):
                        fallback = self._semantic_candidates(term)
                        if not fallback:
                            continue
                        node = fallback[0]
                        node_id = str(node.get("id") or "")
                        if not node_id or node_id in seen_candidate_ids:
                            continue
                        seen_candidate_ids.add(node_id)
                        semantic_hits.append((node, term_index, term))
                    if semantic_hits:
                        unresolved_terms = []

                ranked_hits = exact_hits or semantic_hits
                if ranked_hits:
                    intent_types = {
                        "concern": {"SkinConcern", "Concern"},
                        "ingredient": {"Ingredient"},
                        "care": {"CareNeed", "Care"},
                        "drug": {"Drug", "Medication"},
                    }
                    expected_types = intent_types.get(intent, set())
                    primary_key = normalize_lookup(primary_term)

                    def rank_hit(item: tuple[dict[str, Any], int, str]) -> tuple[int, int, int, int, int]:
                        node, term_index, term = item
                        properties = dict(node.get("properties") or {})
                        labels = [
                            str(node.get("matched_alias") or ""),
                            str(node.get("name") or ""),
                            *[str(value) for value in properties.get("aliases") or []],
                        ]
                        explicit_length = max(
                            (
                                len(key)
                                for key in (normalize_lookup(label) for label in labels)
                                if key and key in normalized_input
                            ),
                            default=0,
                        )
                        term_key = normalize_lookup(term)
                        return (
                            1 if explicit_length else 0,
                            1 if primary_key and term_key == primary_key else 0,
                            1 if str(node.get("type") or "") in expected_types else 0,
                            explicit_length,
                            -term_index,
                        )

                    ranked_hits.sort(key=rank_hit, reverse=True)
                    candidates = [item[0] for item in ranked_hits]
                    strategy = (
                        "terra-query-understanding"
                        if exact_hits
                        else "terra-query-understanding-plus-bge-m3"
                    )

        if not candidates and not bool(interpretation.get("parsed")):
            candidates = self._semantic_candidates(clean_query)
            strategy = "bge-m3-plus-reranker"

        if not candidates:
            intent = str(interpretation.get("intent") or "unknown")
            interpreter_parsed = bool(interpretation.get("parsed"))
            if interpreter_parsed and interpreted_terms:
                code = "knowledge_term_not_indexed"
            elif interpreter_parsed:
                code = "knowledge_query_out_of_scope"
            else:
                code = "knowledge_term_not_resolved"
            raise KnowledgeQueryNotResolved(
                code=code,
                query=clean_query,
                terms=interpreted_terms,
                intent=intent,
                mode=str(interpretation.get("mode") or "single"),
            )
        center_id = str(candidates[0]["id"])
        canonical_name = str(candidates[0].get("name") or "").strip()
        candidate_entities = [
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("name") or ""),
                "type": str(node.get("type") or ""),
            }
            for node in candidates[:4]
            if node.get("id") and node.get("name")
        ]
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
            "interpreted_terms": interpreted_terms,
            "interpreted_query": canonical_name or None,
            "input_was_rewritten": normalize_lookup(canonical_name) != normalize_lookup(clean_query),
            "query_intent": str(interpretation.get("intent") or "unknown"),
            "query_mode": str(interpretation.get("mode") or "single"),
            "candidate_entities": candidate_entities,
            "unresolved_terms": unresolved_terms,
            "ambiguous": len(candidates) > 1 or bool(unresolved_terms),
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
        except KnowledgeQueryNotResolved:
            # A normal, structured product outcome must reach the API route.
            # It is not an infrastructure failure and must not be wrapped as 503.
            raise
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
