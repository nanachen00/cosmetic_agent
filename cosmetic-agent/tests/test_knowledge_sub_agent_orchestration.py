from types import SimpleNamespace

import pytest

import agents.knowledge.knowledge_sub_agent as knowledge_module
from agents.knowledge.knowledge_sub_agent import KnowledgeSubAgent


class _SessionContext:
    def __init__(self, repository):
        self.repository = repository

    def __enter__(self):
        self.repository.events.append("session_enter")
        return self.repository.session_value

    def __exit__(self, exc_type, exc, traceback):
        self.repository.events.append("session_exit")
        return False


class FakeGraphRepository:
    def __init__(self, events, session_value="neo4j-session"):
        self.events = events
        self.session_value = session_value
        self.closed = False

    def session(self):
        self.events.append("session_requested")
        return _SessionContext(self)

    def close(self):
        self.closed = True


def make_agent(events):
    agent = KnowledgeSubAgent.__new__(KnowledgeSubAgent)
    agent.graph_repository = FakeGraphRepository(events)
    agent.neo4j_driver = SimpleNamespace(session=agent.graph_repository.session)
    agent.conversation_history = {}
    agent.max_history_turns = 20
    agent.skin_tool = SimpleNamespace(search_concern=lambda query: None)
    agent.risk_tool = SimpleNamespace(check_ingredient_risk=lambda *args: [])
    return agent


def test_history_is_limited_to_configured_turns():
    agent = make_agent([])
    agent.max_history_turns = 2

    for index in range(6):
        role = "user" if index % 2 == 0 else "assistant"
        agent._append_history("session-1", role, str(index))

    assert agent._get_history("session-1") == [
        {"role": "user", "content": "2"},
        {"role": "assistant", "content": "3"},
        {"role": "user", "content": "4"},
        {"role": "assistant", "content": "5"},
    ]


def test_a2a_orchestration_reuses_intent_and_runs_risk_rules_last(monkeypatch):
    events = []
    agent = make_agent(events)

    monkeypatch.setattr(
        knowledge_module,
        "parse_intent",
        lambda **kwargs: pytest.fail("parsed intent should be reused"),
    )
    monkeypatch.setattr(
        knowledge_module,
        "observe_pipeline_stage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_module,
        "knowledge_intent_parser",
        SimpleNamespace(
            get_ordered_collections=lambda query: [
                "risk_rules",
                "ingredients",
                "skin_concerns",
            ]
        ),
    )
    monkeypatch.setattr(
        knowledge_module,
        "terra_extractor",
        SimpleNamespace(
            extract_ingredient=lambda query: (
                events.append(("extract", query)) or "玻尿酸"
            )
        ),
    )

    def get_node_data(*, session, keyword):
        events.append(("ingredient", session, keyword))
        return {"name_zh": "玻尿酸", "benefits": ["保濕"]}

    monkeypatch.setattr(
        knowledge_module,
        "ingredient_lookup_tool",
        SimpleNamespace(get_node_data=get_node_data),
    )

    class SkinTool:
        def search_concern(self, query):
            events.append(("skin", query))
            return {"name": "敏感肌"}

    class RiskTool:
        def check_ingredient_risk(self, session, ingredient, skin_type):
            events.append(("risk", session, ingredient, skin_type))
            return ["請先局部測試"]

    agent.skin_tool = SkinTool()
    agent.risk_tool = RiskTool()

    result = agent.handle_request(
        user_query="玻尿酸適合敏感肌嗎？",
        is_a2a=True,
        user_skin_type="sensitive",
        parsed_intent=SimpleNamespace(query_expansion=["透明質酸"]),
    )

    assert result["status"] == "success"
    assert result["payload"]["ingredient_info"]["name_zh"] == "玻尿酸"
    assert result["payload"]["skin_concern_info"]["name"] == "敏感肌"
    assert result["payload"]["warnings"] == ["請先局部測試"]
    assert result["payload"]["graph_raw_data"] == {
        "status": "risk_triggered",
        "warnings": ["請先局部測試"],
    }
    assert events == [
        "session_requested",
        "session_enter",
        ("extract", "玻尿酸適合敏感肌嗎？"),
        ("ingredient", "neo4j-session", "玻尿酸"),
        ("skin", "玻尿酸適合敏感肌嗎？"),
        ("risk", "neo4j-session", "玻尿酸", "sensitive"),
        "session_exit",
    ]


def test_follow_up_uses_combined_query_only_for_retrieval(monkeypatch):
    events = []
    agent = make_agent(events)
    agent.conversation_history["chat-1"] = [
        {"role": "user", "content": "玻尿酸適合敏感肌嗎？"}
    ]

    routed_queries = []
    monkeypatch.setattr(
        knowledge_module,
        "knowledge_intent_parser",
        SimpleNamespace(
            get_ordered_collections=lambda query: routed_queries.append(query) or []
        ),
    )
    monkeypatch.setattr(
        knowledge_module,
        "observe_pipeline_stage",
        lambda *args, **kwargs: None,
    )

    on_delta = object()
    generated = {}

    def generate_reply(**kwargs):
        generated.update(kwargs)
        return {"status": "success", "reply": "測試回答"}

    agent._generate_final_pharmacist_reply = generate_reply

    result = agent.handle_request(
        user_query="那油肌呢",
        session_id="chat-1",
        memory_context="既有記憶",
        parsed_intent=SimpleNamespace(query_expansion=[]),
        on_delta=on_delta,
    )

    assert routed_queries == [
        "玻尿酸適合敏感肌嗎？（延伸追問：那油肌呢）"
    ]
    assert generated["query"] == "那油肌呢"
    assert generated["memory_context"] == "既有記憶"
    assert generated["on_delta"] is on_delta
    assert generated["context"] == {
        "graph_raw_data": None,
        "ingredient_info": None,
        "skin_concern_info": None,
        "warnings": [],
    }
    assert result["reply"] == "測試回答"
    assert result["payload"] == generated["context"]
    assert events == ["session_requested", "session_enter", "session_exit"]


def test_empty_graph_results_keep_the_existing_empty_payload(monkeypatch):
    events = []
    agent = make_agent(events)

    monkeypatch.setattr(
        knowledge_module,
        "parse_intent",
        lambda **kwargs: pytest.fail("parsed intent should be reused"),
    )
    monkeypatch.setattr(
        knowledge_module,
        "observe_pipeline_stage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_module,
        "knowledge_intent_parser",
        SimpleNamespace(
            get_ordered_collections=lambda query: [
                "ingredients",
                "skin_concerns",
                "risk_rules",
            ]
        ),
    )
    monkeypatch.setattr(
        knowledge_module,
        "terra_extractor",
        SimpleNamespace(extract_ingredient=lambda query: None),
    )
    monkeypatch.setattr(
        knowledge_module,
        "ingredient_lookup_tool",
        SimpleNamespace(get_node_data=lambda **kwargs: None),
    )

    agent.skin_tool = SimpleNamespace(search_concern=lambda query: None)
    agent.risk_tool = SimpleNamespace(
        check_ingredient_risk=lambda *args: pytest.fail(
            "risk lookup must be skipped without an ingredient"
        )
    )

    result = agent.handle_request(
        user_query="找不到的成分",
        is_a2a=True,
        parsed_intent=SimpleNamespace(query_expansion=[]),
    )

    assert result == {
        "status": "success",
        "payload": {
            "graph_raw_data": None,
            "ingredient_info": None,
            "skin_concern_info": None,
            "warnings": [],
        },
    }
    assert events == ["session_requested", "session_enter", "session_exit"]


def test_graph_tool_failure_still_propagates_and_closes_session(monkeypatch):
    events = []
    agent = make_agent(events)

    monkeypatch.setattr(
        knowledge_module,
        "observe_pipeline_stage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_module,
        "knowledge_intent_parser",
        SimpleNamespace(get_ordered_collections=lambda query: ["ingredients"]),
    )
    monkeypatch.setattr(
        knowledge_module,
        "terra_extractor",
        SimpleNamespace(extract_ingredient=lambda query: "玻尿酸"),
    )

    def fail_lookup(**kwargs):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(
        knowledge_module,
        "ingredient_lookup_tool",
        SimpleNamespace(get_node_data=fail_lookup),
    )
    agent.skin_tool = SimpleNamespace(search_concern=lambda query: None)
    agent.risk_tool = SimpleNamespace(check_ingredient_risk=lambda *args: [])

    with pytest.raises(RuntimeError, match="neo4j down"):
        agent.handle_request(
            user_query="玻尿酸",
            is_a2a=True,
            parsed_intent=SimpleNamespace(query_expansion=[]),
        )

    assert events == ["session_requested", "session_enter", "session_exit"]


def test_missing_parsed_intent_uses_existing_terra_parser_contract(monkeypatch):
    events = []
    agent = make_agent(events)
    parse_calls = []
    routed_queries = []

    def parse_intent(**kwargs):
        parse_calls.append(kwargs)
        return SimpleNamespace(query_expansion=[])

    monkeypatch.setattr(knowledge_module, "parse_intent", parse_intent)
    monkeypatch.setattr(
        knowledge_module,
        "observe_pipeline_stage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_module,
        "knowledge_intent_parser",
        SimpleNamespace(
            get_ordered_collections=lambda query: routed_queries.append(query) or []
        ),
    )
    agent.skin_tool = SimpleNamespace(search_concern=lambda query: None)
    agent.risk_tool = SimpleNamespace(check_ingredient_risk=lambda *args: [])

    result = agent.handle_request(user_query="玻尿酸", is_a2a=True)

    assert parse_calls == [
        {"user_query": "玻尿酸", "model": "gpt-5.6-terra"}
    ]
    assert routed_queries == ["玻尿酸"]
    assert result["status"] == "success"
    assert events == ["session_requested", "session_enter", "session_exit"]
