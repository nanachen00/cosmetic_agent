from pathlib import Path
from agents.product import product_agent


def test_query_embedding_delegates_to_shared_client(monkeypatch):
    expected = [0.25] * 1024
    calls = []

    def fake_embed(query):
        calls.append(query)
        return expected

    monkeypatch.setattr(product_agent._embedding_client, "embed_query", fake_embed)

    assert product_agent.get_query_embedding("保濕精華") == expected
    assert calls == ["保濕精華"]


def test_query_embedding_failure_keeps_public_error_contract(monkeypatch):
    def fail(_query):
        raise product_agent.EmbeddingServiceError("offline")

    monkeypatch.setattr(product_agent._embedding_client, "embed_query", fail)
    monkeypatch.setattr(product_agent, "_log", lambda *args, **kwargs: None)

    try:
        product_agent.get_query_embedding("敏感肌")
    except RuntimeError as exc:
        assert str(exc) == "product embedding service unavailable"
    else:
        raise AssertionError("embedding failure must not load an in-process model")


def test_default_product_path_uses_vector_index_not_legacy_scan():
    source = Path(product_agent.__file__).read_text(encoding="utf-8")
    assert "PRODUCT_USE_LEGACY_CATEGORY_SCAN" not in source
    assert "CYPHER 25" in source
    assert "SEARCH node IN (" in source
    assert "VECTOR INDEX product_vector_index" in source
    assert "db.index.vector.queryNodes" not in source
    assert "WHERE $target_cats IS NULL" in source
    assert 'PRODUCT_CANDIDATE_POOL_SIZE", "40"' in source

def test_skin_concern_path_uses_search_clause_not_deprecated_procedure():
    source = Path("tools/search/skinconcern_tool.py").read_text(encoding="utf-8")
    assert "CYPHER 25" in source
    assert "MATCH (node:SkinConcern)" in source
    assert "VECTOR INDEX skin_concern_vector_index" in source
    assert "db.index.vector.queryNodes" not in source

