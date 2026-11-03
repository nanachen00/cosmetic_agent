from __future__ import annotations

import pytest

from app.services.knowledge_query_pipeline import KnowledgeQueryPipeline


class FakeEmbedder:
    model = "bge-m3"

    def __init__(self):
        self.queries = []

    def embed_query(self, query):
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class FakeReranker:
    model = "bge-reranker-v2-m3"

    def rerank(self, query, documents, *, top_n):
        return [{"index": 0, "score": 0.95}]


class FakeQueryInterpreter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


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
        return [{
            "id": "semantic-node",
            "type": "Ingredient",
            "name": "神經醯胺",
            "description": "屏障支持",
            "properties": {},
            "semantic_score": 0.83,
        }]


class FakeExplorer:
    source_mode = "legacy"
    cloud_only = False

    def __init__(self, matches=()):
        self.seed_repository = FakeSeedRepository(matches)
        self.repository = FakeRepository()

    def neighborhood(self, node_id, *, depth=1, limit=60):
        return {
            "center": {"id": node_id, "name": node_id, "type": "Knowledge"},
            "nodes": [
                {"id": node_id, "name": node_id, "type": "Knowledge"},
                {"id": "target", "name": "照護", "type": "CareNeed"},
            ],
            "edges": [{
                "id": "allowed",
                "source": node_id,
                "target": "target",
                "relation": "RELEVANT_TO_CARE",
                "evidence_status": "verified",
            }],
        }


@pytest.mark.parametrize(
    ("query", "canonical_name", "response"),
    [
        (
            "內包粉次怎麼辦",
            "閉鎖性粉刺",
            '{"terms":["閉鎖性粉刺","白頭粉刺"],"intent":"concern"}',
        ),
        (
            "水揚酸可以改善粉刺嗎",
            "水楊酸",
            '{"terms":["水楊酸"],"intent":"ingredient"}',
        ),
    ],
)
def test_llm_understanding_maps_question_alias_or_typo_to_verified_graph_node(
    query, canonical_name, response,
):
    explorer = FakeExplorer([{
        "id": "verified-node",
        "name": canonical_name,
        "type": "SkinConcern" if "粉刺" in canonical_name else "Ingredient",
        "properties": {},
    }])
    embedder = FakeEmbedder()
    interpreter = FakeQueryInterpreter(response)
    pipeline = KnowledgeQueryPipeline(
        explorer,
        embedding_client=embedder,
        reranker_client=FakeReranker(),
        query_interpreter=interpreter,
        query_interpreter_model="gpt-5.6-terra",
    )

    result = pipeline.resolve(query)

    assert result["center"]["id"] == "verified-node"
    assert result["resolution"]["strategy"] == "terra-query-understanding"
    assert result["resolution"]["input_was_rewritten"] is True
    assert result["resolution"]["interpreted_query"] == canonical_name
    assert embedder.queries == []
    assert query in interpreter.calls[0]["messages"][1]["content"]
    assert interpreter.calls[0]["temperature"] == 0.0


def test_query_interpreter_failure_falls_back_to_existing_semantic_pipeline():
    interpreter = FakeQueryInterpreter(RuntimeError("offline"))
    embedder = FakeEmbedder()
    pipeline = KnowledgeQueryPipeline(
        FakeExplorer(),
        embedding_client=embedder,
        reranker_client=FakeReranker(),
        query_interpreter=interpreter,
    )

    result = pipeline.resolve("修護乾燥屏障")

    assert result["center"]["id"] == "semantic-node"
    assert result["resolution"]["strategy"] == "bge-m3-plus-reranker"
    assert embedder.queries == ["修護乾燥屏障"]
