import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.conversations import create_conversation_router
from app.services.conversation_repository import ConversationRepository
from app.services.conversation_service import ConversationQuotaExceeded


class FakeMemory:
    def __init__(self, **kwargs):
        self.calls = []

    def list_conversations_page(self, *, limit, offset):
        self.calls.append((limit, offset))
        return ([{"conversation_id": "c2"}], 9)

    def count_conversations(self):
        return 9


def test_repository_pushes_pagination_into_memory_adapter():
    memories = []
    def factory(**kwargs):
        memory = FakeMemory(**kwargs)
        memories.append(memory)
        return memory
    repository = ConversationRepository(memory_factory=factory)
    page, total = repository.list(user_id="u1", limit=1, offset=4)
    assert page == [{"conversation_id": "c2"}]
    assert total == 9
    assert memories[0].calls == [(1, 4)]


class StubService:
    max_conversations_per_user = 20
    quota_enforcement_mode = "best_effort_non_atomic"
    def __init__(self):
        self.page_calls = []
        self.reject_create = False
        self.create_ran_without_loop = False
    def list_page(self, *, user_id, limit, offset):
        self.page_calls.append((user_id, limit, offset))
        return ([{"conversation_id": "c3"}], 6)
    def create_draft(self, *, user_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.create_ran_without_loop = True
        else:
            raise AssertionError("create_draft ran on the event-loop thread")
        if self.reject_create:
            raise ConversationQuotaExceeded(current=20, maximum=20)
        return {"conversation_id": "draft", "title": "新對話"}


def build_client(service):
    app = FastAPI()
    app.include_router(create_conversation_router(
        owner_resolver=lambda request, user_id: user_id,
        service=service,
    ))
    return TestClient(app)


def test_create_route_runs_sync_service_outside_event_loop():
    service = StubService()
    response = build_client(service).post(
        "/api/conversations",
        json={"user_id": "u1"},
    )
    assert response.status_code == 200
    assert service.create_ran_without_loop is True


def test_route_uses_page_contract_and_rejects_over_quota_create():
    service = StubService()
    client = build_client(service)
    response = client.get("/api/conversations", params={"user_id": "u1", "limit": 1, "offset": 2})
    assert response.status_code == 200
    assert service.page_calls == [("u1", 1, 2)]
    assert response.json()["total"] == 6
    service.reject_create = True
    response = client.post("/api/conversations", json={"user_id": "u1"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conversation_quota_exceeded"
