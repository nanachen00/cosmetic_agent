from __future__ import annotations

import pytest

from app.services.knowledge_query_pipeline import (
    KnowledgeQueryPipeline,
    KnowledgeQueryUnavailable,
)


class FakeEmbedder:
    model = "bge-m3"

    def __init__(self):
        self.queries = []

    def embed_query(self, query):
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class FailingEmbedder(FakeEmbedder):
    def embed_query(self, query):
        raise RuntimeError("offline")


class FakeReranker:
    model = "bge-reranker-v2-m3"

    def __init__(self):
        self.calls = []

    def rerank(self, query, documents, *, top_n):
        self.calls.append((query, documents, top_n))
        return [{"index": 1, "score": 0.95}, {"index": 0, "score": 0.7}]


class FakeSeedRepository:
    def __init__(self, matches):
        self.matches = matches

    def search(self, query, *, limit=30):
        return list(self.matches)


class FakeRepository:
    def __init__(self):
        self.vectors = []

    def search(self, query, *, limit=30):
        return []

    def semantic_search(self, vector, *, limit=12):
        self.vectors.append((vector, limit))
        return [
            {
                "id": "n1",
                "type": "Ingredient",
                "name": "玻尿酸",
                "description": "補水",
                "properties": {},
                "semantic_score": 0.83,
            },
            {
                "id": "n2",
                "type": "Ingredient",
                "name": "神經醯胺",
                "description": "屏障支持",
                "properties": {},
                "semantic_score": 0.78,
            },
        ]


class FakeExplorer:
    def __init__(self, matches=()):
        self.seed_repository = FakeSeedRepository(matches)
        self.repository = FakeRepository()
        self.explain_result = None

    def neighborhood(self, node_id, *, depth=1, limit=60):
        return {
            "center": {"id": node_id, "name": node_id, "type": "Ingredient"},
            "nodes": [
                {"id": node_id, "name": node_id, "type": "Ingredient"},
                {"id": "target", "name": "保濕", "type": "Function"},
                {"id": "bad", "name": "捏造", "type": "Knowledge"},
            ],
            "edges": [
                {
                    "id": "allowed",
                    "source": node_id,
                    "target": "target",
                    "relation": "HAS_FUNCTION",
                    "evidence_status": "derived",
                    "source_ids": ["classification_rules_v1"],
                },
                {
                    "id": "blocked",
                    "source": node_id,
                    "target": "bad",
                    "relation": "LLM_INVENTED",
                    "evidence_status": "unknown",
                },
            ],
        }

    async def explain(self, node_id):
        return self.explain_result

    @classmethod
    def _fallback(cls, graph, *, reason):
        return {"fallback": True, "reason": reason}


def test_exact_alias_bypasses_gpu_stages_and_filters_relations():
    explorer = FakeExplorer(
        [{
            "id": "ingredient_hyaluronic_acid",
            "name": "Hyaluronic Acid",
            "type": "Ingredient",
            "matched_alias": "玻尿酸",
            "properties": {},
        }]
    )
    embedder = FakeEmbedder()
    reranker = FakeReranker()
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=embedder,
        reranker_client=reranker,
    )

    result = pipeline.resolve("玻尿酸")

    assert result["resolution"]["strategy"] == "exact-alias"
    assert result["resolution"]["canonical_node_id"] == "ingredient_hyaluronic_acid"
    assert [edge["id"] for edge in result["edges"]] == ["allowed"]
    assert result["relation_policy"]["rejected_edge_count"] == 1
    assert result["evidence"]["source_ids"] == ["classification_rules_v1"]
    assert embedder.queries == []
    assert reranker.calls == []


def test_semantic_candidates_are_reranked_before_graph_lookup():
    explorer = FakeExplorer()
    embedder = FakeEmbedder()
    reranker = FakeReranker()
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=embedder,
        reranker_client=reranker,
    )

    result = pipeline.resolve("修護乾燥屏障")

    assert result["center"]["id"] == "n2"
    assert result["resolution"]["strategy"] == "bge-m3-plus-reranker"
    assert result["resolution"]["candidates"][0]["rerank_score"] == 0.95
    assert embedder.queries == ["修護乾燥屏障"]
    assert explorer.repository.vectors == [([0.1, 0.2, 0.3], 12)]


def test_semantic_candidate_internal_id_is_mapped_to_curated_id():
    explorer = FakeExplorer(
        [
            {
                "id": "ingredient_ceramide",
                "name": "Ceramide",
                "type": "Ingredient",
                "matched_alias": "神經醯胺",
                "properties": {"aliases": ["神經醯胺"]},
            }
        ]
    )
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=FakeEmbedder(),
        reranker_client=FakeReranker(),
    )

    result = pipeline.resolve("修護乾燥屏障")

    assert result["center"]["id"] == "ingredient_ceramide"
    assert result["resolution"]["canonical_node_id"] == "ingredient_ceramide"
    assert result["resolution"]["candidates"][0]["node_id"] == "ingredient_ceramide"


def test_semantic_path_fails_closed_without_reranker():
    pipeline = KnowledgeQueryPipeline(
        FakeExplorer(),
        embedding_client=FakeEmbedder(),
        reranker_client=None,
    )

    with pytest.raises(KnowledgeQueryUnavailable, match="reranker_not_configured"):
        pipeline.resolve("沒有精確別名")


@pytest.mark.asyncio
async def test_explanation_is_rejected_if_citations_escape_allowlist():
    explorer = FakeExplorer(
        [{
            "id": "n1",
            "name": "玻尿酸",
            "type": "Ingredient",
            "matched_alias": "玻尿酸",
            "properties": {},
        }]
    )
    explorer.explain_result = {
        "fallback": False,
        "citations": {"edge_ids": ["not-allowed"]},
    }
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=FakeEmbedder(),
        reranker_client=FakeReranker(),
    )
    graph = pipeline.resolve("玻尿酸")

    result = await pipeline.explain(graph)

    assert result == {"fallback": True, "reason": "relation_policy_violation"}


@pytest.mark.asyncio
async def test_async_admission_fails_fast_when_capacity_is_full():
    explorer = FakeExplorer(
        [{
            "id": "n1",
            "name": "玻尿酸",
            "type": "Ingredient",
            "matched_alias": "玻尿酸",
            "properties": {},
        }]
    )
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=FakeEmbedder(),
        reranker_client=FakeReranker(),
        max_running=1,
        max_queued=0,
        queue_timeout_seconds=0.01,
    )
    occupied = await pipeline.admission.reserve()
    try:
        with pytest.raises(KnowledgeQueryUnavailable, match="knowledge_query_busy"):
            await pipeline.resolve_async("玻尿酸")
    finally:
        await occupied.release()


def test_embedding_failure_is_mapped_to_stable_unavailable_error():
    pipeline = KnowledgeQueryPipeline(
        FakeExplorer(),
        embedding_client=FailingEmbedder(),
        reranker_client=FakeReranker(),
    )

    with pytest.raises(KnowledgeQueryUnavailable, match="embedding_unavailable"):
        pipeline.resolve("自然語意問題")


def test_required_redis_admission_refuses_memory_fallback(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS", "1")
    monkeypatch.delenv("KNOWLEDGE_QUERY_ADMISSION_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("RERANKER_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="no Redis URL"):
        KnowledgeQueryPipeline.from_env(FakeExplorer())
