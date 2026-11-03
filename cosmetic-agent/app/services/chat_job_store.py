from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from copy import deepcopy
from typing import Any


class ChatJobStore:
    """Short-lived in-process request status and SSE replay buffer."""

    def __init__(self, *, ttl_seconds: float = 600.0, max_entries: int = 2048) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _discard_expired(self, now: float) -> None:
        expired = [
            request_id
            for request_id, job in self._jobs.items()
            if now - float(job["updated_at"]) > self._ttl_seconds
        ]
        for request_id in expired:
            self._jobs.pop(request_id, None)

        if len(self._jobs) <= self._max_entries:
            return
        oldest = sorted(
            self._jobs,
            key=lambda request_id: float(self._jobs[request_id]["updated_at"]),
        )
        for request_id in oldest[: len(self._jobs) - self._max_entries]:
            self._jobs.pop(request_id, None)

    async def start(
        self,
        request_id: str,
        *,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        now = time.time()
        async with self._lock:
            self._discard_expired(now)
            if request_id in self._jobs:
                return False
            self._jobs[request_id] = {
                "requestId": request_id,
                "status": "running",
                "conversationId": conversation_id,
                "user_id": user_id,
                "result": None,
                "error": None,
                "updated_at": now,
                "_events": [],
                "_next_event_id": 0,
            }
        return True

    async def complete(
        self,
        request_id: str,
        *,
        user_id: str,
        result: dict[str, Any],
    ) -> None:
        await self._update(
            request_id,
            user_id=user_id,
            status="completed",
            result=deepcopy(result),
            error=None,
        )

    async def fail(self, request_id: str, *, user_id: str, message: str) -> None:
        await self._update(
            request_id,
            user_id=user_id,
            status="failed",
            result=None,
            error=message,
        )

    async def _update(self, request_id: str, *, user_id: str, **changes: Any) -> None:
        async with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job["user_id"] != user_id:
                return
            job.update(changes)
            job["updated_at"] = time.time()

    async def append_event(
        self,
        request_id: str,
        *,
        user_id: str,
        event: str,
        payload: dict[str, Any],
        max_events: int = 256,
    ) -> int | None:
        async with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job["user_id"] != user_id:
                return None
            event_id = int(job.get("_next_event_id", 0)) + 1
            job["_next_event_id"] = event_id
            events = job.setdefault("_events", [])
            events.append(
                {
                    "id": event_id,
                    "event": event,
                    "payload": deepcopy(payload),
                }
            )
            if len(events) > max_events:
                del events[: len(events) - max_events]
            job["updated_at"] = time.time()
            return event_id

    async def events_after(
        self,
        request_id: str,
        *,
        user_id: str,
        last_event_id: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job["user_id"] != user_id:
                return []
            return deepcopy(
                [
                    item
                    for item in job.get("_events", [])
                    if int(item.get("id", 0)) > last_event_id
                ]
            )

    async def get(self, request_id: str, *, user_id: str) -> dict[str, Any] | None:
        now = time.time()
        async with self._lock:
            self._discard_expired(now)
            job = self._jobs.get(request_id)
            if job is None or job["user_id"] != user_id:
                return None
            public_job = {
                key: value
                for key, value in job.items()
                if key not in {"user_id", "updated_at"} and not key.startswith("_")
            }
            return deepcopy(public_job)


class RedisChatJobStore:
    """Redis-backed job and SSE replay store shared by all API workers."""

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_seconds: float = 600.0,
        key_prefix: str = "blink:chat-job",
    ) -> None:
        from redis.asyncio import Redis

        self._ttl_seconds = max(1, math.ceil(ttl_seconds))
        self._key_prefix = key_prefix.rstrip(":")
        self._client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )

    def _token(self, request_id: str) -> str:
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest()

    def _job_key(self, request_id: str) -> str:
        return f"{self._key_prefix}:{self._token(request_id)}"

    def _events_key(self, request_id: str) -> str:
        return f"{self._job_key(request_id)}:events"

    def _sequence_key(self, request_id: str) -> str:
        return f"{self._job_key(request_id)}:sequence"

    async def start(
        self,
        request_id: str,
        *,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        job = {
            "requestId": request_id,
            "status": "running",
            "conversationId": conversation_id,
            "user_id": user_id,
            "result": None,
            "error": None,
            "updated_at": time.time(),
        }
        created = await self._client.set(
            self._job_key(request_id),
            json.dumps(job, ensure_ascii=False, separators=(",", ":")),
            ex=self._ttl_seconds,
            nx=True,
        )
        return bool(created)

    async def complete(
        self,
        request_id: str,
        *,
        user_id: str,
        result: dict[str, Any],
    ) -> None:
        await self._update(
            request_id,
            user_id=user_id,
            status="completed",
            result=deepcopy(result),
            error=None,
        )

    async def fail(self, request_id: str, *, user_id: str, message: str) -> None:
        await self._update(
            request_id,
            user_id=user_id,
            status="failed",
            result=None,
            error=message,
        )

    async def _update(self, request_id: str, *, user_id: str, **changes: Any) -> None:
        key = self._job_key(request_id)
        raw = await self._client.get(key)
        if raw is None:
            return
        job = json.loads(raw)
        if job.get("user_id") != user_id:
            return
        job.update(changes)
        job["updated_at"] = time.time()
        await self._client.set(
            key,
            json.dumps(job, ensure_ascii=False, separators=(",", ":")),
            ex=self._ttl_seconds,
        )

    async def append_event(
        self,
        request_id: str,
        *,
        user_id: str,
        event: str,
        payload: dict[str, Any],
        max_events: int = 256,
    ) -> int | None:
        job_key = self._job_key(request_id)
        raw = await self._client.get(job_key)
        if raw is None or json.loads(raw).get("user_id") != user_id:
            return None
        events_key = self._events_key(request_id)
        sequence_key = self._sequence_key(request_id)
        event_id = int(await self._client.incr(sequence_key))
        item = json.dumps(
            {"id": event_id, "event": event, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        pipe = self._client.pipeline(transaction=True)
        pipe.rpush(events_key, item)
        pipe.ltrim(events_key, -max_events, -1)
        pipe.expire(events_key, self._ttl_seconds)
        pipe.expire(sequence_key, self._ttl_seconds)
        pipe.expire(job_key, self._ttl_seconds)
        await pipe.execute()
        return event_id

    async def events_after(
        self,
        request_id: str,
        *,
        user_id: str,
        last_event_id: int = 0,
    ) -> list[dict[str, Any]]:
        raw = await self._client.get(self._job_key(request_id))
        if raw is None or json.loads(raw).get("user_id") != user_id:
            return []
        items = await self._client.lrange(self._events_key(request_id), 0, -1)
        events = [json.loads(item) for item in items]
        return [item for item in events if int(item.get("id", 0)) > last_event_id]

    async def get(self, request_id: str, *, user_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(self._job_key(request_id))
        if raw is None:
            return None
        job = json.loads(raw)
        if job.get("user_id") != user_id:
            return None
        return {
            key: deepcopy(value)
            for key, value in job.items()
            if key not in {"user_id", "updated_at"}
        }


JobStore = ChatJobStore | RedisChatJobStore


def create_chat_job_store() -> JobStore:
    redis_url = os.getenv("CHAT_JOB_REDIS_URL", os.getenv("REDIS_URL", "")).strip()
    require_redis = os.getenv("CHAT_JOB_STORE_REQUIRE_REDIS", "0") == "1"
    if redis_url:
        try:
            return RedisChatJobStore(
                redis_url,
                ttl_seconds=float(os.getenv("CHAT_JOB_TTL_SECONDS", "600")),
            )
        except (ImportError, ModuleNotFoundError) as error:
            if require_redis:
                raise RuntimeError("Redis asyncio support is required for the chat job store") from error
    if require_redis:
        raise RuntimeError(
            "CHAT_JOB_STORE_REQUIRE_REDIS=1 requires CHAT_JOB_REDIS_URL or REDIS_URL"
        )
    return ChatJobStore(ttl_seconds=float(os.getenv("CHAT_JOB_TTL_SECONDS", "600")))


chat_job_store = create_chat_job_store()
