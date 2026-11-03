"""Bounded FIFO admission control for expensive chat generations."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional


class AdmissionFull(RuntimeError):
    """Raised when both execution slots and the waiting queue are full."""


class AdmissionTimeout(RuntimeError):
    """Raised when a queued request waits longer than the configured limit."""


@dataclass
class AdmissionTicket:
    controller: "ChatAdmissionController"
    ticket_id: int | str
    reserved_at: float
    acquired: bool
    released: bool = False

    async def wait(self, timeout: float) -> float:
        """Wait for an execution slot and return queue time in seconds."""
        if self.acquired:
            return 0.0
        await self.controller._wait(self, timeout)
        return max(0.0, time.perf_counter() - self.reserved_at)

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.controller._release(self)


class ChatAdmissionController:
    """Process-local FIFO controller with finite execution and queue capacity."""

    def __init__(
        self,
        max_running: int,
        max_queued: int,
        *,
        on_state: Optional[Callable[[int, int], None]] = None,
        on_overload: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.max_running = max(1, int(max_running))
        self.max_queued = max(0, int(max_queued))
        self._active = 0
        self._next_ticket_id = 0
        self._queue: Deque[int] = deque()
        self._condition = asyncio.Condition()
        self._on_state = on_state
        self._on_overload = on_overload

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._queue)

    def _publish_state(self) -> None:
        if self._on_state is not None:
            self._on_state(self._active, len(self._queue))

    def _publish_overload(self, reason: str) -> None:
        if self._on_overload is not None:
            self._on_overload(reason)

    async def reserve(self) -> AdmissionTicket:
        async with self._condition:
            self._next_ticket_id += 1
            ticket_id = self._next_ticket_id
            now = time.perf_counter()
            if self._active < self.max_running and not self._queue:
                self._active += 1
                ticket = AdmissionTicket(self, ticket_id, now, acquired=True)
                self._publish_state()
                return ticket
            if len(self._queue) >= self.max_queued:
                self._publish_overload("queue_full")
                raise AdmissionFull("chat queue is full")
            self._queue.append(ticket_id)
            ticket = AdmissionTicket(self, ticket_id, now, acquired=False)
            self._publish_state()
            return ticket

    async def _wait(self, ticket: AdmissionTicket, timeout: float) -> None:
        async def wait_for_turn() -> None:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: (
                        bool(self._queue)
                        and self._queue[0] == ticket.ticket_id
                        and self._active < self.max_running
                    )
                )
                self._queue.popleft()
                self._active += 1
                ticket.acquired = True
                self._publish_state()

        try:
            await asyncio.wait_for(wait_for_turn(), timeout=max(0.01, timeout))
        except asyncio.TimeoutError as error:
            async with self._condition:
                try:
                    self._queue.remove(ticket.ticket_id)
                except ValueError:
                    pass
                self._publish_state()
                self._condition.notify_all()
            self._publish_overload("queue_timeout")
            raise AdmissionTimeout("chat queue wait timed out") from error

    async def _release(self, ticket: AdmissionTicket) -> None:
        async with self._condition:
            if ticket.acquired:
                self._active = max(0, self._active - 1)
                ticket.acquired = False
            else:
                try:
                    self._queue.remove(ticket.ticket_id)
                except ValueError:
                    pass
            self._publish_state()
            self._condition.notify_all()


class RedisChatAdmissionController:
    """Redis-backed FIFO admission controller shared by all API workers."""

    _RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local ticket = ARGV[2]
local max_running = tonumber(ARGV[3])
local max_queued = tonumber(ARGV[4])
local lease_until = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local active = redis.call('ZCARD', KEYS[2])
local queued = redis.call('ZCARD', KEYS[1])
if active < max_running and queued == 0 then
  redis.call('ZADD', KEYS[2], lease_until, ticket)
  return {1, 0, active + 1}
end
if queued >= max_queued then
  return {-1, queued, active}
end
redis.call('ZADD', KEYS[1], 'NX', now, ticket)
queued = redis.call('ZCARD', KEYS[1])
return {0, queued, active}
"""

    _ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local ticket = ARGV[2]
local max_running = tonumber(ARGV[3])
local lease_until = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZSCORE', KEYS[2], ticket) then
  return {1, redis.call('ZCARD', KEYS[1]), redis.call('ZCARD', KEYS[2])}
end
local rank = redis.call('ZRANK', KEYS[1], ticket)
local active = redis.call('ZCARD', KEYS[2])
if rank == 0 and active < max_running then
  redis.call('ZREM', KEYS[1], ticket)
  redis.call('ZADD', KEYS[2], lease_until, ticket)
  return {1, redis.call('ZCARD', KEYS[1]), active + 1}
end
return {0, redis.call('ZCARD', KEYS[1]), active}
"""

    def __init__(
        self,
        redis_url: str,
        max_running: int,
        max_queued: int,
        *,
        key_prefix: str = "blink:chat:admission",
        lease_seconds: float = 900.0,
        poll_seconds: float = 0.1,
        on_state: Optional[Callable[[int, int], None]] = None,
        on_overload: Optional[Callable[[str, None], None]] = None,
        redis_client: Any = None,
    ) -> None:
        if redis_client is None:
            from redis.asyncio import Redis

            redis_client = Redis.from_url(redis_url, decode_responses=True)
        self._redis = redis_client
        self.max_running = max(1, int(max_running))
        self.max_queued = max(0, int(max_queued))
        self._queue_key = f"{key_prefix}:queue"
        self._active_key = f"{key_prefix}:active"
        self._lease_seconds = max(5.0, float(lease_seconds))
        self._poll_seconds = max(0.02, float(poll_seconds))
        self._on_state = on_state
        self._on_overload = on_overload
        self._active_snapshot = 0
        self._queued_snapshot = 0
        self._renewals: dict[str, asyncio.Task[None]] = {}

    @property
    def active(self) -> int:
        return self._active_snapshot

    @property
    def queued(self) -> int:
        return self._queued_snapshot

    def _publish_state(self, queued: int, active: int) -> None:
        self._queued_snapshot = int(queued)
        self._active_snapshot = int(active)
        if self._on_state is not None:
            self._on_state(self._active_snapshot, self._queued_snapshot)

    def _publish_overload(self, reason: str) -> None:
        if self._on_overload is not None:
            self._on_overload(reason)

    async def reserve(self) -> AdmissionTicket:
        ticket_id = uuid.uuid4().hex
        now = time.time()
        acquired, queued, active = await self._redis.eval(
            self._RESERVE_SCRIPT,
            2,
            self._queue_key,
            self._active_key,
            now,
            ticket_id,
            self.max_running,
            self.max_queued,
            now + self._lease_seconds,
        )
        self._publish_state(int(queued), int(active))
        if int(acquired) < 0:
            self._publish_overload("queue_full")
            raise AdmissionFull("chat admission queue is full")
        ticket = AdmissionTicket(
            self,
            ticket_id,
            time.perf_counter(),
            acquired=bool(int(acquired)),
        )
        if ticket.acquired:
            self._start_renewal(ticket)
        return ticket

    async def _wait(self, ticket: AdmissionTicket, timeout: float) -> None:
        deadline = time.monotonic() + max(0.01, float(timeout))
        ticket_id = str(ticket.ticket_id)
        while True:
            now = time.time()
            acquired, queued, active = await self._redis.eval(
                self._ACQUIRE_SCRIPT,
                2,
                self._queue_key,
                self._active_key,
                now,
                ticket_id,
                self.max_running,
                now + self._lease_seconds,
            )
            self._publish_state(int(queued), int(active))
            if int(acquired) == 1:
                ticket.acquired = True
                self._start_renewal(ticket)
                return
            if time.monotonic() >= deadline:
                await self._redis.zrem(self._queue_key, ticket_id)
                self._publish_overload("queue_timeout")
                raise AdmissionTimeout("chat queue wait timed out")
            await asyncio.sleep(self._poll_seconds)

    def _start_renewal(self, ticket: AdmissionTicket) -> None:
        ticket_id = str(ticket.ticket_id)
        if ticket_id in self._renewals:
            return

        async def renew() -> None:
            try:
                while True:
                    await asyncio.sleep(self._lease_seconds / 3.0)
                    await self._redis.zadd(
                        self._active_key,
                        {ticket_id: time.time() + self._lease_seconds},
                        xx=True,
                    )
            except asyncio.CancelledError:
                return

        self._renewals[ticket_id] = asyncio.create_task(renew())

    async def _release(self, ticket: AdmissionTicket) -> None:
        ticket_id = str(ticket.ticket_id)
        renewal = self._renewals.pop(ticket_id, None)
        if renewal is not None:
            renewal.cancel()
        pipe = self._redis.pipeline(transaction=False)
        pipe.zrem(self._active_key, ticket_id)
        pipe.zrem(self._queue_key, ticket_id)
        await pipe.execute()
        active, queued = await asyncio.gather(
            self._redis.zcard(self._active_key),
            self._redis.zcard(self._queue_key),
        )
        self._publish_state(int(queued), int(active))


def create_chat_admission_controller(
    *,
    max_running: int,
    max_queued: int,
    on_state: Optional[Callable[[int, int], None]] = None,
    on_overload: Optional[Callable[[str, None], None]] = None,
) -> ChatAdmissionController | RedisChatAdmissionController:
    redis_url = (
        os.getenv("CHAT_ADMISSION_REDIS_URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
    )
    require_redis = os.getenv("CHAT_ADMISSION_REQUIRE_REDIS", "0") == "1"
    if redis_url:
        try:
            return RedisChatAdmissionController(
                redis_url,
                max_running,
                max_queued,
                key_prefix=os.getenv(
                    "CHAT_ADMISSION_KEY_PREFIX", "blink:chat:admission"
                ),
                lease_seconds=float(
                    os.getenv("CHAT_ADMISSION_LEASE_SECONDS", "900")
                ),
                poll_seconds=float(
                    os.getenv("CHAT_ADMISSION_POLL_SECONDS", "0.1")
                ),
                on_state=on_state,
                on_overload=on_overload,
            )
        except (ImportError, ModuleNotFoundError) as error:
            if require_redis:
                raise RuntimeError(
                    "Redis asyncio support is required for chat admission"
                ) from error
    if require_redis:
        raise RuntimeError(
            "CHAT_ADMISSION_REQUIRE_REDIS=1 but no Redis URL is configured"
        )
    return ChatAdmissionController(
        max_running=max_running,
        max_queued=max_queued,
        on_state=on_state,
        on_overload=on_overload,
    )
