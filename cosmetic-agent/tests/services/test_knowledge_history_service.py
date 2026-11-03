from app.services.knowledge_history_service import KnowledgeConversationHistory


def test_history_keeps_sessions_isolated_and_returns_same_list():
    history = KnowledgeConversationHistory()

    first = history.get("first")
    first.append({"role": "user", "content": "問題"})

    assert history.get("first") is first
    assert history.get("second") == []


def test_append_limits_history_to_complete_configured_turn_count():
    history = KnowledgeConversationHistory(max_history_turns=2)

    for index in range(6):
        role = "user" if index % 2 == 0 else "assistant"
        history.append("chat", role, str(index))

    assert history.get("chat") == [
        {"role": "user", "content": "2"},
        {"role": "assistant", "content": "3"},
        {"role": "user", "content": "4"},
        {"role": "assistant", "content": "5"},
    ]


def test_last_user_query_and_clear_preserve_existing_behavior():
    history = KnowledgeConversationHistory()
    history.append("chat", "user", "第一題")
    history.append("chat", "assistant", "回答")
    history.append("chat", "user", "第二題")

    assert history.last_user_query("chat") == "第二題"

    history.clear("chat")

    assert history.get("chat") == []
    assert history.last_user_query("missing") == ""
