from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.knowledge_explorer import create_knowledge_explorer_router
from app.services.knowledge_query_pipeline import KnowledgeQueryUnavailable


class FakeService:
    def status(self):
        return {"ok": True, "feature_enabled": True, "taide_enabled": False}

    def search(self, query, *, limit=12):
        return [{"id": "n1", "name": query, "type": "Ingredient", "description": "", "properties": {}}]

    def neighborhood(self, node_id, *, limit=18):
        return {"center": {"id": node_id}, "nodes": [], "edges": []}

    def explore(self, query, *, depth=2, limit=60):
        return {
            "center": {"id": "concern_rosacea", "name": "酒糟性皮膚炎"},
            "nodes": [],
            "edges": [],
            "groups": [],
            "resolution": {"query": query, "strategy": "normalized-alias"},
        }

    async def explain(self, node_id, *, viewer_context=None, focus="overview"):
        return {"ok": True, "node_id": node_id, "fallback": True, "explanation": "圖譜說明"}


class FakeIngredientRepository:
    def ingredient_graph(self, query, *, limit=18):
        if query == "missing":
            return None
        if query == "error":
            raise RuntimeError("database unavailable")
        return {"center": {"id": "i1", "type": "Ingredient", "name": query}, "nodes": [{"id": "i1", "type": "Ingredient", "name": query}], "edges": [], "resolution": {"strategy": "neo4j_ingredient"}}

    def ingredient_mentions(self, messages, *, limit=2):
        return ["菸鹼醯胺"] if any("菸鹼醯胺" in text for text in messages) else []


class FakePipeline:
    def __init__(self, *, fail=False):
        self.fail = fail

    async def resolve_async(self, query, *, depth=1, limit=60):
        if self.fail:
            raise KnowledgeQueryUnavailable("knowledge_query_busy")
        return {
            "center": {"id": "ingredient_hyaluronic_acid", "name": "玻尿酸"},
            "nodes": [{"id": "ingredient_hyaluronic_acid", "name": "玻尿酸"}],
            "edges": [],
            "evidence": {"edge_ids": [], "source_ids": [], "statuses": []},
            "resolution": {"query": query, "strategy": "exact-alias"},
        }

    async def explain(self, graph):
        return {"fallback": True, "explanation": "固定圖譜說明"}


def build_client(pipeline=None):
    app = FastAPI()
    service = FakeService()
    pipeline = pipeline or FakePipeline()
    app.include_router(
        create_knowledge_explorer_router(
            lambda: service, lambda: pipeline, lambda: FakeIngredientRepository()
        )
    )
    return TestClient(app)


def test_feature_flag_hides_routes_by_default(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_GRAPH_ENABLED", raising=False)
    response = build_client().get("/api/knowledge/status")
    assert response.status_code == 404


def test_enabled_routes_return_injected_service_data(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    client = build_client()
    assert client.get("/api/knowledge/status").status_code == 200
    search = client.get("/api/knowledge/search", params={"q": "維他命C"})
    assert search.status_code == 200
    assert search.json()["nodes"][0]["name"] == "維他命C"
    explore = client.get("/api/knowledge/explore", params={"q": "酒糟肌"})
    assert explore.status_code == 200
    assert explore.json()["center"]["id"] == "concern_rosacea"
    explain = client.post("/api/knowledge/explain", json={"node_id": "n1"})
    assert explain.status_code == 200
    assert explain.json()["fallback"] is True


def test_query_route_requires_its_own_feature_flag(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    monkeypatch.delenv("KNOWLEDGE_QUERY_PIPELINE_ENABLED", raising=False)
    response = build_client().post(
        "/api/knowledge/query", json={"query": "玻尿酸"}
    )
    assert response.status_code == 404


def test_query_route_returns_graph_and_explanation(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_QUERY_PIPELINE_ENABLED", "1")
    response = build_client().post(
        "/api/knowledge/query",
        json={"query": "玻尿酸", "explain": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolution"]["strategy"] == "exact-alias"
    assert body["explanation"]["fallback"] is True


def test_query_route_defaults_to_graph_first_without_waiting_for_explanation(
    monkeypatch,
):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_QUERY_PIPELINE_ENABLED", "1")
    response = build_client().post(
        "/api/knowledge/query", json={"query": "玻尿酸"}
    )
    assert response.status_code == 200
    assert "explanation" not in response.json()


def test_query_route_returns_503_when_admission_is_full(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    monkeypatch.setenv("KNOWLEDGE_QUERY_PIPELINE_ENABLED", "1")
    response = build_client(FakePipeline(fail=True)).post(
        "/api/knowledge/query", json={"query": "自然語意問題"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "knowledge_query_busy"


def test_ingredient_suggestions_only_uses_ingredient_questions(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    client = build_client()
    response = client.post(
        "/api/knowledge/ingredient-suggestions",
        json={"messages": ["請問菸鹼醯胺有什麼功效？", "今天泛紅"]},
    )
    assert response.status_code == 200
    assert response.json()["ingredients"] == ["菸鹼醯胺"]
    ignored = client.post(
        "/api/knowledge/ingredient-suggestions",
        json={"messages": ["我今天使用菸鹼醯胺"]},
    )
    assert ignored.status_code == 200
    assert ignored.json()["ingredients"] == []


def test_ingredient_suggestions_obeys_feature_flag_and_request_limit(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_GRAPH_ENABLED", raising=False)
    client = build_client()
    assert client.post(
        "/api/knowledge/ingredient-suggestions",
        json={"messages": ["請問菸鹼醯胺適合我嗎？"]},
    ).status_code == 404
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    assert client.post(
        "/api/knowledge/ingredient-suggestions",
        json={"messages": ["請問菸鹼醯胺適合我嗎？"] * 41},
    ).status_code == 422


def test_ingredient_graph_uses_dedicated_repository_without_seed(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    response = build_client().get("/api/knowledge/ingredients/graph", params={"q": "玻尿酸"})
    assert response.status_code == 200
    assert response.json()["center"]["name"] == "玻尿酸"
    assert response.json()["resolution"]["strategy"] == "neo4j_ingredient"
    assert response.json()["edges"] == []


def test_ingredient_graph_handles_missing_and_unavailable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    client = build_client()
    assert client.get("/api/knowledge/ingredients/graph", params={"q": "missing"}).status_code == 404
    assert client.get("/api/knowledge/ingredients/graph", params={"q": "error"}).status_code == 503


def test_ingredient_graph_obeys_feature_flag_and_limits(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_GRAPH_ENABLED", raising=False)
    client = build_client()
    assert client.get("/api/knowledge/ingredients/graph", params={"q": "玻尿酸"}).status_code == 404
    monkeypatch.setenv("KNOWLEDGE_GRAPH_ENABLED", "1")
    assert client.get("/api/knowledge/ingredients/graph", params={"q": "x" * 81}).status_code == 422
    assert client.get("/api/knowledge/ingredients/graph", params={"q": "玻尿酸", "limit": 31}).status_code == 422
