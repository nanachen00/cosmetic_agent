from types import SimpleNamespace

import pytest

from app.services.conversation_service import (
    ConversationQuotaExceeded,
    ConversationService,
)


class FakeMemory:
    def __init__(self) -> None:
        self.deleted = None

    def list_conversations(self):
        return [{"conversation_id": "c1"}]

    def list_conversations_page(self, *, limit, offset):
        conversations = self.list_conversations()
        return conversations[offset : offset + limit], len(conversations)

    def count_conversations(self):
        return len(self.list_conversations())

    def rename_conversation(self, conversation_id, title):
        return {"conversation_id": conversation_id, "title": title}

    def delete_conversation(self, conversation_id):
        self.deleted = conversation_id
        return True

    def get_history(self, *, lastn):
        return [{"role": "user", "content": str(lastn)}]


def build_service(monkeypatch):
    monkeypatch.setenv("AUTH_STORAGE_BACKEND", "neo4j")
    memory = FakeMemory()
    sessions = {("u1", "normalized-c1"): object()}
    service = ConversationService(
        agent_provider=lambda **kwargs: SimpleNamespace(memory=memory),
        normalize_conversation_id=lambda value: f"normalized-{value}",
        sessions=sessions,
        conversation_limit_status=lambda messages, policy: {"count": len(messages), "policy": policy},
        conversation_limit_policy="limit-policy",
    )
    return service, memory, sessions


def test_conversation_service_preserves_lifecycle(monkeypatch):
    service, memory, sessions = build_service(monkeypatch)
    assert service.list(user_id="u1") == [{"conversation_id": "c1"}]
    draft = service.create_draft(user_id="u1")
    assert len(draft["conversation_id"]) == 32
    assert draft["title"] == "新對話"
    assert service.rename(user_id="u1", conversation_id="c1", title="新標題") == {
        "conversation_id": "c1",
        "title": "新標題",
    }
    assert service.delete(user_id="u1", conversation_id="c1") == "normalized-c1"
    assert memory.deleted == "normalized-c1"
    assert sessions == {}


def test_conversation_service_exposes_bounded_account_quota(monkeypatch):
    service, _, _ = build_service(monkeypatch)
    assert service.max_conversations_per_user == 100

    monkeypatch.setenv("CONVERSATION_MAX_PER_USER", "12")
    assert service.max_conversations_per_user == 20

    monkeypatch.setenv("CONVERSATION_MAX_PER_USER", "999")
    assert service.max_conversations_per_user == 100


def test_conversation_service_history_includes_limit(monkeypatch):
    service, _, _ = build_service(monkeypatch)
    messages, limit = service.history(user_id="u1", conversation_id="c1", lastn=25)
    assert messages == [{"role": "user", "content": "25"}]
    assert limit == {"count": 1, "policy": "limit-policy"}


def test_conversation_service_forwards_pagination_to_repository(monkeypatch):
    service, _, _ = build_service(monkeypatch)
    calls = []

    class Repository:
        def list(self, *, user_id, limit, offset):
            calls.append((user_id, limit, offset))
            return ([{"conversation_id": "c3"}], 7)

    service._repository = Repository()
    page, total = service.list_page(user_id="u1", limit=1, offset=2)

    assert page == [{"conversation_id": "c3"}]
    assert total == 7
    assert calls == [("u1", 1, 2)]


def test_create_draft_rejects_account_at_quota(monkeypatch):
    service, _, _ = build_service(monkeypatch)
    monkeypatch.setenv("CONVERSATION_MAX_PER_USER", "20")

    class Repository:
        def count(self, *, user_id):
            assert user_id == "u1"
            return 20

    service._repository = Repository()

    with pytest.raises(ConversationQuotaExceeded) as captured:
        service.create_draft(user_id="u1")

    assert captured.value.current == 20
    assert captured.value.maximum == 20
    assert service.quota_enforcement_mode == "best_effort_non_atomic"
