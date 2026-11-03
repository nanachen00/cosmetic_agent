import pytest

from app.services.knowledge_explorer_service import KnowledgeExplorerService


class FakeRepository:
    def search(self, query, *, limit=12):
        return [{"id": "n1", "type": "Ingredient", "name": query, "description": "", "properties": {}}]

    def neighborhood(self, node_id, *, limit=18):
        if node_id == "missing":
            return None
        return {
            "center": {
                "id": node_id,
                "type": "Ingredient",
                "name": "維他命C",
                "description": "抗氧化成分",
                "properties": {"name_zh": "維他命C"},
            },
            "nodes": [
                {"id": node_id, "type": "Ingredient", "name": "維他命C", "description": "抗氧化成分", "properties": {}},
                {"id": "c1", "type": "SkinConcern", "name": "暗沉", "description": "", "properties": {}},
            ],
            "edges": [
                {
                    "id": "e1",
                    "source": node_id,
                    "target": "c1",
                    "relation": "SUITABLE_FOR_CONCERN",
                    "relation_label": "照護關聯",
                    "evidence_status": "needs-review",
                    "properties": {},
                }
            ],
        }


class FakeSeedRepository:
    def status(self):
        return {"ok": True}

    def search(self, query, *, limit=12):
        return []

    def neighborhood(self, node_id, *, depth=2, limit=60):
        return None

    def explore(self, query, *, depth=2, limit=60):
        return None


class FakeSnapshotRepository(FakeRepository):
    def status(self):
        return {"available": True, "node_count": 2}


class FakeTerraClient:
    calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return (
            "簡單說｜維他命C是常見的抗氧化成分。\n"
            "重要關聯｜圖譜顯示它與暗沉照護有關。\n"
            "可以留意｜不同膚況仍要觀察使用感受。"
        )


class FailingTerraClient:
    def complete(self, **kwargs):
        raise RuntimeError("provider unavailable")


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "簡單說｜維他命C與暗沉照護有關。"}}]}


class FakeTaideClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, **kwargs):
        assert url == "http://127.0.0.1:8200/v1/chat/completions"
        assert kwargs["json"]["model"] == "taide-12b"
        return FakeResponse()


def build_service(**kwargs):
    return KnowledgeExplorerService(
        repository=FakeRepository(),
        seed_repository=FakeSeedRepository(),
        **kwargs,
    )


def test_json_source_keeps_snapshot_repository_compatibility(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLORER_SOURCE", "json")
    snapshot = FakeSnapshotRepository()
    service = KnowledgeExplorerService(
        repository=FakeRepository(),
        seed_repository=snapshot,
    )

    assert service.repository is snapshot
    assert service.seed_repository is snapshot
    assert service.status()["data_source"] == "json-file"


@pytest.mark.asyncio
async def test_explain_uses_terra_by_default(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_EXPLAINER_PROVIDER", raising=False)
    FakeTerraClient.calls.clear()
    service = build_service(terra_client_factory=FakeTerraClient)
    result = await service.explain("n1")
    assert result["fallback"] is False
    assert result["provider"] == "terra"
    assert result["model"] == "gpt-5.6-terra"
    assert result["citations"]["node_ids"] == ["c1", "n1"]
    assert result["citations"]["edge_ids"] == ["e1"]
    assert "資料提醒｜部分內容仍在整理確認中。" in result["explanation"]
    assert result["sections"][0]["title"] == "簡單說"
    assert all(len(item["content"]) <= 64 for item in result["sections"])
    assert FakeTerraClient.calls[0]["temperature"] == 0.15


@pytest.mark.asyncio
async def test_explain_personalizes_prompt_focus_and_cache_key(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_EXPLAINER_PROVIDER", raising=False)
    FakeTerraClient.calls.clear()
    service = build_service(terra_client_factory=FakeTerraClient)

    first = await service.explain(
        "n1",
        focus="care",
        viewer_context={"skin_type": "油性", "concerns": ["粉刺"]},
    )
    second = await service.explain(
        "n1",
        focus="safety",
        viewer_context={"skin_type": "敏感", "allergens": ["香精"]},
    )

    assert first["personalized"] is True
    assert first["focus"] == "care"
    assert second["focus"] == "safety"
    assert len(FakeTerraClient.calls) == 2
    first_prompt = FakeTerraClient.calls[0]["messages"][1]["content"]
    second_prompt = FakeTerraClient.calls[1]["messages"][1]["content"]
    assert "explanation_focus=care" in first_prompt
    assert 'section_labels=["照護方向", "怎麼做", "可以留意"]' in first_prompt
    assert '"skin_type":"油性"' in first_prompt
    assert "explanation_focus=safety" in second_prompt
    assert '"allergens":["香精"]' in second_prompt


@pytest.mark.asyncio
async def test_explain_can_use_explicit_experimental_taide(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLAINER_PROVIDER", "taide")
    monkeypatch.setenv("KNOWLEDGE_TAIDE_ENABLED", "1")
    service = build_service(client_factory=FakeTaideClient)
    result = await service.explain("n1")
    assert result["fallback"] is False
    assert result["provider"] == "taide-vllm"
    assert result["model"] == "taide-12b"


@pytest.mark.asyncio
async def test_taide_requires_both_provider_and_experimental_flag(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLAINER_PROVIDER", "taide")
    monkeypatch.delenv("KNOWLEDGE_TAIDE_ENABLED", raising=False)
    service = build_service(client_factory=FakeTaideClient)
    result = await service.explain("n1")
    assert result["fallback"] is True
    assert result["provider"] == "graph-fallback"
    assert result["reason"] == "taide_disabled"


@pytest.mark.asyncio
async def test_explainer_off_uses_deterministic_fallback(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLAINER_PROVIDER", "off")
    service = build_service(terra_client_factory=FakeTerraClient)
    result = await service.explain("n1")
    assert result["fallback"] is True
    assert result["provider"] == "graph-fallback"
    assert result["reason"] == "explainer_off"
    assert "抗氧化" in result["explanation"]


@pytest.mark.asyncio
async def test_terra_failure_falls_back_without_calling_taide(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLAINER_PROVIDER", "terra")
    monkeypatch.setenv("KNOWLEDGE_TAIDE_ENABLED", "1")
    service = build_service(terra_client_factory=FailingTerraClient)
    result = await service.explain("n1")
    assert result["fallback"] is True
    assert result["reason"] == "terra_unavailable"
    assert result["provider"] == "graph-fallback"


@pytest.mark.asyncio
async def test_missing_node_returns_none(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_EXPLAINER_PROVIDER", "terra")
    service = build_service(terra_client_factory=FakeTerraClient)
    assert await service.explain("missing") is None


def test_status_reports_terra_default(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_EXPLAINER_PROVIDER", raising=False)
    service = build_service(terra_client_factory=FakeTerraClient)
    status = service.status()
    assert status["provider"] == "terra"
    assert status["model"] == "gpt-5.6-terra"
    assert status["taide_experimental"] is True


def test_explainer_formats_human_readable_focus_sections():
    result = KnowledgeExplorerService._clean_model_text(
        "閉鎖性粉刺是膚色或白色的小凸起。\n"
        "圖譜把它連到毛孔堵塞這項形成因素。\n"
        "若你在意反覆粉刺，可先看溫和清潔的照護關係。\n"
        "避免摳抓或擠壓。\n"
        "目前來源沒有提供專業協助門檻。\n"
        "第六段不應顯示。",
        "overview",
    )
    assert result.splitlines() == [
        "簡單說｜閉鎖性粉刺是膚色或白色的小凸起。",
        "重要關聯｜圖譜把它連到毛孔堵塞這項形成因素。",
        "可以留意｜若你在意反覆粉刺，可先看溫和清潔的照護關係。",
    ]
    assert "已審核關係" not in result


def test_review_notice_cleanup_never_removes_other_readable_sections():
    graph = FakeRepository().neighborhood("n1")
    graph["edges"][0]["evidence_status"] = "verified"
    result = KnowledgeExplorerService._ensure_required_notice(
        "簡單說｜這是第一個重點。\n"
        "重要關聯｜這是第二個重點。\n"
        "可以留意｜這句尚待人工複核。",
        graph,
    )
    result = KnowledgeExplorerService._relabel_sections(result, "overview")

    assert result.splitlines() == [
        "簡單說｜這是第一個重點。",
        "重要關聯｜這是第二個重點。",
    ]
    assert "尚待人工複核" not in result


def test_ingredient_mentions_only_queries_neo4j_and_deduplicates_messages():
    from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository

    class FakeGraph:
        def session(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, cypher, **params):
            assert "MATCH (ingredient:Ingredient)" in cypher
            assert "ingredient.aliases" in cypher
            assert params["questions"] == ["請問水楊酸適合我嗎？"]
            assert params["limit"] == 2
            return [{"name": "水楊酸"}]

    repository = KnowledgeExplorerRepository(graph_repository=FakeGraph())
    result = repository.ingredient_mentions(
        ["請問水楊酸適合我嗎？", "請問水楊酸適合我嗎？"], limit=2
    )
    assert result == ["水楊酸"]
    assert repository.ingredient_mentions([]) == []


def test_ingredient_graph_uses_live_neo4j_and_marks_unreviewed_edges():
    from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository

    class Rows(list):
        def single(self):
            return self[0] if self else None

    class FakeGraph:
        def session(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, cypher, **params):
            assert "mongo_id" not in cypher
            if "MATCH (n:Ingredient)-[r]-(m)" not in cypher:
                assert params["parameters"]["query"] == "水楊酸"
                return Rows([{
                    "id": "i1", "labels": ["Ingredient"],
                    "properties": {"name_zh": "水楊酸", "mongo_id": "private-i1"},
                }])
            assert params["node_id"] == "i1"
            return Rows([
                {
                    "neighbor_id": "i2", "neighbor_labels": ["Ingredient"],
                    "neighbor_properties": {"name_zh": "某成分", "mongo_id": "private-i2"},
                    "source_id": "i1", "target_id": "i2",
                    "relation": "CONFLICTS_WITH",
                    "relation_properties": {"source": "https://example.org/source", "description_zh": "來源說明"},
                },
                {
                    "neighbor_id": "c1", "neighbor_labels": ["SkinConcern"],
                    "neighbor_properties": {"name": "痘痘"},
                    "source_id": "i1", "target_id": "c1",
                    "relation": "SUITABLE_FOR_CONCERN", "relation_properties": {},
                },
            ])

    repository = KnowledgeExplorerRepository(graph_repository=FakeGraph())
    graph = repository.ingredient_graph("水楊酸")
    assert graph["resolution"]["strategy"] == "neo4j_ingredient"
    assert [node["id"] for node in graph["nodes"]] == ["i1", "i2", "c1"]
    assert graph["edges"][0]["relation_label"] == "不建議同時使用"
    assert graph["edges"][0]["evidence_status"] == "documented"
    assert graph["edges"][1]["relation_label"] == "膚況資訊"
    assert graph["edges"][1]["evidence_status"] == "needs-review"
    assert graph["edges"][1]["source"] == "i1"
    assert graph["edges"][1]["target"] == "c1"
    assert all("mongo_id" not in node["properties"] for node in graph["nodes"])


def test_ingredient_graph_returns_none_without_a_live_match():
    from app.repositories.knowledge_explorer_repository import KnowledgeExplorerRepository

    class EmptyRows:
        def single(self):
            return None

    class EmptyGraph:
        def session(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, *_args, **_kwargs):
            return EmptyRows()

    repository = KnowledgeExplorerRepository(graph_repository=EmptyGraph())
    assert repository.ingredient_graph("不存在的成分") is None
    assert repository.ingredient_graph("  ") is None
