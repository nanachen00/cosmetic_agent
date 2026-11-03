from pathlib import Path

from app.security.conversation_limit import ConversationLimitPolicy, conversation_limit_status


def _messages(user_count: int, *, content: str = "hello") -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for index in range(user_count):
        history.append({"role": "user", "content": f"{content}-{index}"})
        history.append({"role": "assistant", "content": "ok"})
    return history


def test_below_warning_threshold_is_open() -> None:
    policy = ConversationLimitPolicy(max_user_messages=40, max_tokens=30_000, warning_percent=80)
    status = conversation_limit_status(_messages(31), policy=policy)
    assert status["blocked"] is False
    assert status["warning"] is False
    assert status["remaining_user_messages"] == 9


def test_warning_starts_at_configured_percentage() -> None:
    policy = ConversationLimitPolicy(max_user_messages=40, max_tokens=30_000, warning_percent=80)
    status = conversation_limit_status(_messages(32), policy=policy)
    assert status["blocked"] is False
    assert status["warning"] is True
    assert status["reason"] is None


def test_message_limit_blocks_at_exact_boundary() -> None:
    policy = ConversationLimitPolicy(max_user_messages=40, max_tokens=30_000, warning_percent=80)
    status = conversation_limit_status(_messages(39), policy=policy, incoming_user_text="last")
    assert status["blocked"] is True
    assert status["reason"] == "messages"
    assert status["remaining_user_messages"] == 0
    assert status["message"] == "對話內容已額滿，請開新對話。"


def test_token_limit_blocks_independently() -> None:
    policy = ConversationLimitPolicy(max_user_messages=40, max_tokens=10, warning_percent=80)
    status = conversation_limit_status([{"role": "user", "content": "x" * 200}], policy=policy)
    assert status["blocked"] is True
    assert status["reason"] == "tokens"
    assert status["remaining_tokens"] == 0


def test_chat_api_wires_server_authoritative_limit() -> None:
    source = Path("api/api_server.py").read_text(encoding="utf-8")
    route_source = Path("app/api/routes/conversations.py").read_text(encoding="utf-8")
    chat_route_source = Path("app/api/routes/chat.py").read_text(encoding="utf-8")
    service_source = Path("app/services/conversation_service.py").read_text(encoding="utf-8")
    assert "def _enforce_conversation_limit(" in source
    assert "def _completed_conversation_limit(" in source
    assert '"code": "conversation_limit_reached"' in source
    assert "conversation_limit_status=conversation_limit_status" in source
    assert "self._conversation_limit_status(" in service_source
    assert '"conversation_limit": limit' in route_source
    assert source.count("enforce_conversation_limit=_enforce_conversation_limit") == 1
    assert chat_route_source.count("await deps.enforce_conversation_limit(") == 3
