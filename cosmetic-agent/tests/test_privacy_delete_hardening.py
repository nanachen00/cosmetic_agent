from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.conversations import create_conversation_router
from app.services.conversation_service import ConversationService
from tools.getzep.zep_memory_adapter import (
    ConversationStorageError,
    ZepMemoryAdapter,
)


def test_zep_failure_keeps_conversation_index_visible() -> None:
    adapter = object.__new__(ZepMemoryAdapter)
    adapter.user_id = "owner-1"
    adapter.conversation_id = "conversation-1"
    adapter._normalize_conversation_id = lambda value: value
    adapter._build_thread_id = lambda **_kwargs: "thread-1"
    adapter._get_user_metadata = lambda: {
        "conversations": [{"conversation_id": "conversation-1"}],
        "profile": {"skin_type": "normal"},
    }
    saved_metadata = []
    adapter._save_user_metadata = saved_metadata.append

    def fail_delete(**_kwargs):
        raise RuntimeError("provider-internal-id-must-not-leak")

    adapter.client = SimpleNamespace(
        thread=SimpleNamespace(delete=fail_delete),
    )

    with pytest.raises(ConversationStorageError, match="對話刪除暫時無法完成"):
        adapter.delete_conversation("conversation-1")

    assert saved_metadata == []


def test_service_keeps_session_when_primary_delete_fails(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_STORAGE_BACKEND", "neo4j")
    sessions = {("owner-1", "conversation-1"): object()}

    class FailingMemory:
        def delete_conversation(self, _conversation_id):
            raise ConversationStorageError("對話刪除暫時無法完成，請稍後再試。")

    service = ConversationService(
        agent_provider=lambda **_kwargs: SimpleNamespace(memory=FailingMemory()),
        normalize_conversation_id=lambda value: value,
        sessions=sessions,
        conversation_limit_status=lambda *_args, **_kwargs: {},
        conversation_limit_policy=None,
    )

    with pytest.raises(ConversationStorageError):
        service.delete(user_id="owner-1", conversation_id="conversation-1")

    assert ("owner-1", "conversation-1") in sessions


def test_delete_route_returns_fixed_retryable_error() -> None:
    class FailingService:
        max_conversations_per_user = 100
        quota_enforcement_mode = "best_effort_non_atomic"

        def delete(self, **_kwargs):
            raise RuntimeError("provider-internal-id-must-not-leak")

    app = FastAPI()
    app.include_router(
        create_conversation_router(
            owner_resolver=lambda _request, _proposed: "owner-1",
            service=FailingService(),
        )
    )

    response = TestClient(app).delete(
        "/api/conversations/conversation-1?user_id=owner-1"
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "對話刪除暫時無法完成，請稍後再試。"
    }
    assert "provider-internal-id" not in response.text
