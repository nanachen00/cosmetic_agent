import asyncio

from app.services.chat_job_store import ChatJobStore


def test_chat_job_store_tracks_completion_and_isolates_users():
    async def scenario():
        store = ChatJobStore(ttl_seconds=60)
        await store.start(
            "request-1",
            user_id="user-1",
            conversation_id="conversation-1",
        )

        running = await store.get("request-1", user_id="user-1")
        assert running == {
            "requestId": "request-1",
            "status": "running",
            "conversationId": "conversation-1",
            "result": None,
            "error": None,
        }
        assert await store.get("request-1", user_id="user-2") is None

        result = {"reply": "done"}
        await store.complete("request-1", user_id="user-1", result=result)
        result["reply"] = "mutated"

        completed = await store.get("request-1", user_id="user-1")
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["result"] == {"reply": "done"}

    asyncio.run(scenario())


def test_chat_job_store_discards_expired_jobs():
    async def scenario():
        store = ChatJobStore(ttl_seconds=-1)
        await store.start(
            "request-2",
            user_id="user-1",
            conversation_id="conversation-1",
        )
        assert await store.get("request-2", user_id="user-1") is None

    asyncio.run(scenario())


def test_chat_job_store_replays_events_and_rejects_duplicate_start():
    async def scenario():
        store = ChatJobStore(ttl_seconds=60)
        assert await store.start(
            "request-replay",
            user_id="user-1",
            conversation_id="conversation-1",
        ) is True
        assert await store.start(
            "request-replay",
            user_id="user-1",
            conversation_id="conversation-1",
        ) is False

        first_id = await store.append_event(
            "request-replay",
            user_id="user-1",
            event="delta",
            payload={"text": "hello"},
        )
        second_id = await store.append_event(
            "request-replay",
            user_id="user-1",
            event="done",
            payload={"reply": "hello"},
        )

        assert (first_id, second_id) == (1, 2)
        assert await store.events_after(
            "request-replay", user_id="user-2", last_event_id=0
        ) == []
        assert await store.events_after(
            "request-replay", user_id="user-1", last_event_id=1
        ) == [
            {"id": 2, "event": "done", "payload": {"reply": "hello"}}
        ]

    asyncio.run(scenario())
