import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes.chat import ChatRouteDependencies, create_chat_router
from app.schemas.chat import ChatRequest, normalize_conversation_id


def test_chat_request_normalizes_identity_fields():
    request = ChatRequest(message="hello", session_id="guest", conversation_id="   ")
    assert request.uid == "guest"
    assert request.cid == "default"
    assert normalize_conversation_id(" abc ") == "abc"


def test_chat_router_preserves_operations():
    noop = lambda *args, **kwargs: None
    deps = ChatRouteDependencies(*([noop] * 11))
    router = create_chat_router(deps)
    operations = sorted(
        (method, route.path)
        for route in router.routes
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    )
    assert operations == [
        ("GET", "/api/chat/jobs/{request_id}"),
        ("POST", "/api/chat/message"),
        ("POST", "/api/chat/message/stream"),
        ("POST", "/api/v1/chat"),
        ("POST", "/api/v1/session/clear"),
    ]


def _stream_endpoint(router):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/chat/message/stream"
    )


def _request(*headers: tuple[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/message/stream",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in headers
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


def _deps(begin_chat_security):
    def fail(*args, **kwargs):
        raise AssertionError("unexpected dependency call")

    async def async_fail(*args, **kwargs):
        raise AssertionError("unexpected async dependency call")

    return ChatRouteDependencies(
        chat_user_id=lambda request, requested_user_id: "user-1",
        begin_chat_security=begin_chat_security,
        get_agent_with_profile=fail,
        enforce_conversation_limit=async_fail,
        guard_conversation_creation=async_fail,
        reserve_chat_ticket=async_fail,
        wait_for_chat_ticket=async_fail,
        completed_conversation_limit=fail,
        finish_chat_security=async_fail,
        stage_to_route=fail,
        get_agent=fail,
    )


@pytest.mark.asyncio
async def test_stream_reconnect_replays_existing_job_before_duplicate_guard(monkeypatch):
    class ExistingJobStore:
        def __init__(self):
            self.start_called = False

        async def get(self, request_id, *, user_id):
            assert request_id == "request-1"
            assert user_id == "user-1"
            return {"status": "completed"}

        async def events_after(self, request_id, *, user_id, last_event_id):
            assert last_event_id == 1
            return [
                {
                    "id": 2,
                    "event": "done",
                    "payload": {"message": "cached"},
                }
            ]

        async def start(self, *args, **kwargs):
            self.start_called = True
            raise AssertionError("reconnect must not start a second job")

    async def duplicate_guard(*args, **kwargs):
        raise AssertionError("reconnect must bypass duplicate admission")

    store = ExistingJobStore()
    monkeypatch.setattr("app.api.routes.chat.chat_job_store", store)
    endpoint = _stream_endpoint(create_chat_router(_deps(duplicate_guard)))

    response = await endpoint(
        ChatRequest(message="hello", session_id="user-1", conversation_id="chat-1"),
        _request(("Idempotency-Key", "request-1"), ("Last-Event-ID", "1")),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(
        chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks
    ).decode()

    assert "id: 2" in body
    assert "event: done" in body
    assert not store.start_called


@pytest.mark.asyncio
async def test_stream_duplicate_without_last_event_id_still_returns_409():
    async def duplicate_guard(*args, **kwargs):
        raise HTTPException(
            status_code=409, detail="This message request was already accepted"
        )

    endpoint = _stream_endpoint(create_chat_router(_deps(duplicate_guard)))
    with pytest.raises(HTTPException) as caught:
        await endpoint(
            ChatRequest(message="hello", session_id="user-1", conversation_id="chat-1"),
            _request(("Idempotency-Key", "request-1")),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "This message request was already accepted"
