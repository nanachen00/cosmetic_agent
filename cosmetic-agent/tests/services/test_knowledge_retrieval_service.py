from types import SimpleNamespace

from app.services.knowledge_retrieval_service import KnowledgeRetrievalService


class FakeRepository:
    def __init__(self, events):
        self.events = events

    def session(self):
        repository = self

        class Context:
            def __enter__(self):
                repository.events.append("session_enter")
                return "neo4j-session"

            def __exit__(self, exc_type, exc, traceback):
                repository.events.append("session_exit")
                return False

        return Context()


def test_retrieval_uses_query_expansion_and_runs_risk_last():
    events = []

    def lookup(*, session, keyword):
        events.append(("ingredient", session, keyword))
        return {"name_zh": "玻尿酸"}

    def risk(session, ingredient, skin_type):
        events.append(("risk", session, ingredient, skin_type))
        return []

    service = KnowledgeRetrievalService(
        graph_repository=FakeRepository(events),
        ingredient_extractor=SimpleNamespace(
            extract_ingredient=lambda query: None
        ),
        ingredient_lookup=SimpleNamespace(get_node_data=lookup),
        skin_tool=SimpleNamespace(search_concern=lambda query: None),
        risk_tool=SimpleNamespace(check_ingredient_risk=risk),
        observe_stage=lambda *args, **kwargs: None,
    )

    result = service.retrieve(
        query="保濕成分",
        ordered_collections=["risk_rules", "ingredients"],
        intent_result=SimpleNamespace(query_expansion=["透明質酸"]),
        user_skin_type="dry",
    )

    assert events == [
        "session_enter",
        ("ingredient", "neo4j-session", "透明質酸"),
        ("risk", "neo4j-session", "玻尿酸", "dry"),
        "session_exit",
    ]
    assert result["ingredient_info"] == {"name_zh": "玻尿酸"}
    assert result["graph_raw_data"] == {
        "status": "risk_triggered",
        "warnings": [],
    }


def test_order_collections_preserves_order_and_deduplicates_risk_tail():
    assert KnowledgeRetrievalService.order_collections(
        ["risk_rules", "skin_concerns", "risk_rules", "ingredients"]
    ) == ["skin_concerns", "ingredients", "risk_rules"]


def test_empty_context_returns_fresh_warning_lists():
    first = KnowledgeRetrievalService.empty_context()
    second = KnowledgeRetrievalService.empty_context()

    first["warnings"].append("only first")

    assert second["warnings"] == []
