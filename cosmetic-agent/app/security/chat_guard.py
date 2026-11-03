"""Distributed abuse controls for paid chat endpoints.

The guard deliberately stores only hashes of account/IP identifiers.  Redis is
used when configured so limits and locks are shared by every worker.  The
bounded in-process backend exists for local development only; production can
set ``CHAT_LIMIT_REQUIRE_REDIS=1`` to fail closed if Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence
from uuid import UUID, uuid4

try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover - exercised on minimal local installs
    redis_async = None


Limit = tuple[int, int]
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
_LOGGER = logging.getLogger("blink.chat_security")


class ChatGuardError(RuntimeError):
    """Base class for expected guard rejections."""


class ChatRateLimitExceeded(ChatGuardError):
    def __init__(self, scope: str, retry_after: int) -> None:
        self.scope = scope
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"chat limit exceeded: {scope}")


class ChatConcurrentRequest(ChatGuardError):
    def __init__(self, retry_after: int = 5) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__("another request is already running")


class DuplicateChatRequest(ChatGuardError):
    def __init__(self, different_payload: bool = False) -> None:
        self.different_payload = different_payload
        super().__init__(
            "idempotency key was reused with different content"
            if different_payload
            else "request was already accepted"
        )


class ChatGuardUnavailable(ChatGuardError):
    """Raised when production requires Redis and it cannot be reached."""


@dataclass
class ChatGuardTicket:
    lock_key: str
    lock_nonce: str
    backend: str
    renew_task: Optional[asyncio.Task] = None


@dataclass(frozen=True)
class _Counter:
    key: str
    amount: int
    limit: int
    ttl: int
    scope: str


_COUNTER_SCRIPT = """
for i, key in ipairs(KEYS) do
  local offset = (i - 1) * 3
  local amount = tonumber(ARGV[offset + 1])
  local limit = tonumber(ARGV[offset + 2])
  local current = tonumber(redis.call('GET', key) or '0')
  if current + amount > limit then
    local ttl = redis.call('TTL', key)
    if ttl < 1 then ttl = tonumber(ARGV[offset + 3]) end
    return {0, i, ttl}
  end
end
for i, key in ipairs(KEYS) do
  local offset = (i - 1) * 3
  local amount = tonumber(ARGV[offset + 1])
  local ttl = tonumber(ARGV[offset + 3])
  local value = redis.call('INCRBY', key, amount)
  if value == amount then redis.call('EXPIRE', key, ttl) end
end
return {1, 0, 0}
"""

_COMPARE_DELETE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_COMPARE_EXPIRE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def valid_request_id(value: str) -> bool:
    return bool(_REQUEST_ID_RE.fullmatch((value or "").strip()))


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent upper estimate for mixed CJK text."""
    encoded_bytes = len((text or "").encode("utf-8"))
    return max(1, math.ceil(encoded_bytes / 3))


class ChatGuard:
    """Atomic rate, budget, idempotency, and per-account concurrency guard."""

    def __init__(
        self,
        *,
        redis_url: Optional[str] = None,
        require_redis: Optional[bool] = None,
        key_prefix: str = "blink:chat:v1",
        account_limits: Optional[Sequence[Limit]] = None,
        guest_limits: Optional[Sequence[Limit]] = None,
        ip_limits: Optional[Sequence[Limit]] = None,
        conversation_create_account_limits: Optional[Sequence[Limit]] = None,
        conversation_create_ip_limits: Optional[Sequence[Limit]] = None,
        account_daily_tokens: Optional[int] = None,
        guest_daily_tokens: Optional[int] = None,
        global_daily_requests: Optional[int] = None,
        global_daily_tokens: Optional[int] = None,
        reserved_output_tokens: Optional[int] = None,
        estimated_micro_usd_per_1k_tokens: Optional[int] = None,
        account_daily_micro_usd: Optional[int] = None,
        global_daily_micro_usd: Optional[int] = None,
        concurrency_lease_seconds: Optional[int] = None,
        idempotency_ttl: Optional[int] = None,
        memory_max_entries: int = 20_000,
        warning_percent: Optional[int] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis_url = redis_url if redis_url is not None else os.getenv("REDIS_URL", "").strip()
        self.require_redis = (
            require_redis
            if require_redis is not None
            else os.getenv("CHAT_LIMIT_REQUIRE_REDIS", "0").strip().lower() in {"1", "true", "yes"}
        )
        self.key_prefix = key_prefix.rstrip(":")
        self.account_limits = tuple(account_limits or (
            (_env_int("CHAT_ACCOUNT_PER_MINUTE", 12, 1), 60),
            (_env_int("CHAT_ACCOUNT_PER_HOUR", 120, 1), 3600),
            (_env_int("CHAT_ACCOUNT_PER_DAY", 300, 1), 86400),
        ))
        self.guest_limits = tuple(guest_limits or (
            (_env_int("CHAT_GUEST_PER_MINUTE", 6, 1), 60),
            (_env_int("CHAT_GUEST_PER_HOUR", 30, 1), 3600),
            (_env_int("CHAT_GUEST_PER_DAY", 60, 1), 86400),
        ))
        self.ip_limits = tuple(ip_limits or (
            (_env_int("CHAT_IP_PER_MINUTE", 30, 1), 60),
            (_env_int("CHAT_IP_PER_HOUR", 300, 1), 3600),
            (_env_int("CHAT_IP_PER_DAY", 1000, 1), 86400),
        ))
        self.conversation_create_account_limits = tuple(
            conversation_create_account_limits
            or (
                (_env_int("CHAT_CONVERSATION_CREATE_ACCOUNT_PER_MINUTE", 5, 1), 60),
                (_env_int("CHAT_CONVERSATION_CREATE_ACCOUNT_PER_HOUR", 20, 1), 3600),
                (_env_int("CHAT_CONVERSATION_CREATE_ACCOUNT_PER_DAY", 50, 1), 86400),
            )
        )
        self.conversation_create_ip_limits = tuple(
            conversation_create_ip_limits
            or (
                (_env_int("CHAT_CONVERSATION_CREATE_IP_PER_MINUTE", 20, 1), 60),
                (_env_int("CHAT_CONVERSATION_CREATE_IP_PER_HOUR", 60, 1), 3600),
                (_env_int("CHAT_CONVERSATION_CREATE_IP_PER_DAY", 200, 1), 86400),
            )
        )
        self.account_daily_tokens = account_daily_tokens if account_daily_tokens is not None else _env_int("CHAT_ACCOUNT_DAILY_TOKENS", 300_000, 1)
        self.guest_daily_tokens = guest_daily_tokens if guest_daily_tokens is not None else _env_int("CHAT_GUEST_DAILY_TOKENS", 60_000, 1)
        self.global_daily_requests = global_daily_requests if global_daily_requests is not None else _env_int("CHAT_GLOBAL_DAILY_REQUESTS", 5_000, 1)
        self.global_daily_tokens = global_daily_tokens if global_daily_tokens is not None else _env_int("CHAT_GLOBAL_DAILY_TOKENS", 3_000_000, 1)
        self.reserved_output_tokens = reserved_output_tokens if reserved_output_tokens is not None else _env_int("CHAT_RESERVED_OUTPUT_TOKENS", 400, 1)
        self.estimated_micro_usd_per_1k_tokens = estimated_micro_usd_per_1k_tokens if estimated_micro_usd_per_1k_tokens is not None else _env_int("CHAT_ESTIMATED_MICRO_USD_PER_1K_TOKENS", 0)
        self.account_daily_micro_usd = account_daily_micro_usd if account_daily_micro_usd is not None else _env_int("CHAT_ACCOUNT_DAILY_MICRO_USD", 0)
        self.global_daily_micro_usd = global_daily_micro_usd if global_daily_micro_usd is not None else _env_int("CHAT_GLOBAL_DAILY_MICRO_USD", 0)
        self.concurrency_lease_seconds = concurrency_lease_seconds if concurrency_lease_seconds is not None else _env_int("CHAT_CONCURRENCY_LEASE_SECONDS", 300, 30)
        self.idempotency_ttl = idempotency_ttl if idempotency_ttl is not None else _env_int("CHAT_IDEMPOTENCY_TTL_SECONDS", 3600, 60)
        self.memory_max_entries = max(100, int(memory_max_entries))
        self.warning_percent = max(50, min(99, warning_percent if warning_percent is not None else _env_int("CHAT_BUDGET_WARNING_PERCENT", 80)))
        self.clock = clock
        self._redis = None
        self._redis_failed = False
        self._memory_lock = asyncio.Lock()
        self._memory_counters: dict[str, tuple[int, float]] = {}
        self._memory_idempotency: dict[str, tuple[str, float]] = {}
        self._memory_locks: dict[str, tuple[str, float]] = {}
        self._memory_warned: dict[str, float] = {}

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def _redis_client(self):
        if not self.redis_url or self._redis_failed:
            return None
        if redis_async is None:
            if self.require_redis:
                raise ChatGuardUnavailable("redis package is unavailable")
            self._redis_failed = True
            return None
        if self._redis is None:
            self._redis = redis_async.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=2.0,
                health_check_interval=30,
            )
        return self._redis

    async def _backend(self):
        client = self._redis_client()
        if client is None:
            if self.require_redis:
                raise ChatGuardUnavailable("Redis is required for chat")
            return "memory", None
        try:
            await client.ping()
            return "redis", client
        except Exception as error:
            if self.require_redis:
                raise ChatGuardUnavailable("Redis is unavailable") from error
            self._redis_failed = True
            return "memory", None

    def _window_counter(self, scope: str, identity_hash: str, seconds: int, amount: int, limit: int) -> _Counter:
        now = int(self.clock())
        bucket = now // seconds
        ttl = seconds - (now % seconds) + 2
        return _Counter(
            key=f"{self.key_prefix}:count:{scope}:{identity_hash}:{seconds}:{bucket}",
            amount=max(1, int(amount)),
            limit=max(1, int(limit)),
            ttl=max(2, ttl),
            scope=scope,
        )

    def _counters(self, *, user_id: str, ip: str, is_guest: bool, reserved_tokens: int) -> list[_Counter]:
        user_hash = self._hash(user_id)
        ip_hash = self._hash(ip or "unknown")
        result: list[_Counter] = []
        for limit, seconds in (self.guest_limits if is_guest else self.account_limits):
            result.append(self._window_counter("guest" if is_guest else "account", user_hash, seconds, 1, limit))
        for limit, seconds in self.ip_limits:
            result.append(self._window_counter("ip", ip_hash, seconds, 1, limit))
        result.append(self._window_counter("global_requests", "all", 86400, 1, self.global_daily_requests))
        result.append(self._window_counter("guest_tokens" if is_guest else "account_tokens", user_hash, 86400, reserved_tokens, self.guest_daily_tokens if is_guest else self.account_daily_tokens))
        result.append(self._window_counter("global_tokens", "all", 86400, reserved_tokens, self.global_daily_tokens))
        if self.estimated_micro_usd_per_1k_tokens > 0:
            estimated_cost = max(1, math.ceil(reserved_tokens * self.estimated_micro_usd_per_1k_tokens / 1000))
            if self.account_daily_micro_usd > 0:
                result.append(self._window_counter("account_cost", user_hash, 86400, estimated_cost, self.account_daily_micro_usd))
            if self.global_daily_micro_usd > 0:
                result.append(self._window_counter("global_cost", "all", 86400, estimated_cost, self.global_daily_micro_usd))
        return result

    async def begin(
        self,
        *,
        user_id: str,
        ip: str,
        request_id: str,
        route: str,
        conversation_id: str,
        payload_hash: str,
        is_guest: bool,
        input_tokens: int,
    ) -> ChatGuardTicket:
        backend, client = await self._backend()
        user_hash = self._hash(user_id)
        idem_hash = self._hash(f"{user_id}\n{route}\n{conversation_id}\n{request_id}")
        idem_key = f"{self.key_prefix}:idem:{idem_hash}"
        fingerprint = payload_hash.strip().lower()
        lock_key = f"{self.key_prefix}:active:{user_hash}"
        nonce = secrets.token_urlsafe(24)
        reserved_tokens = max(1, int(input_tokens)) + self.reserved_output_tokens
        counters = self._counters(user_id=user_id, ip=ip, is_guest=is_guest, reserved_tokens=reserved_tokens)

        if backend == "redis":
            assert client is not None
            try:
                created = await client.set(idem_key, fingerprint, nx=True, ex=self.idempotency_ttl)
                if not created:
                    existing = await client.get(idem_key)
                    raise DuplicateChatRequest(existing != fingerprint)
                locked = await client.set(lock_key, nonce, nx=True, ex=self.concurrency_lease_seconds)
                if not locked:
                    await client.eval(_COMPARE_DELETE_SCRIPT, 1, idem_key, fingerprint)
                    raise ChatConcurrentRequest()
                argv: list[int] = []
                for item in counters:
                    argv.extend((item.amount, item.limit, item.ttl))
                result = await client.eval(_COUNTER_SCRIPT, len(counters), *[item.key for item in counters], *argv)
                if int(result[0]) != 1:
                    failed_index = max(1, int(result[1])) - 1
                    await client.eval(_COMPARE_DELETE_SCRIPT, 1, lock_key, nonce)
                    await client.eval(_COMPARE_DELETE_SCRIPT, 1, idem_key, fingerprint)
                    raise ChatRateLimitExceeded(counters[failed_index].scope, int(result[2]))
                try:
                    await self._warn_redis_budgets(client, counters)
                except Exception:
                    # Monitoring must never invalidate a reservation that has
                    # already been atomically charged.
                    _LOGGER.warning("chat_budget_warning_check_failed")
            except ChatGuardError:
                raise
            except Exception as error:
                if self.require_redis:
                    raise ChatGuardUnavailable("Redis chat guard failed") from error
                raise ChatGuardUnavailable("Chat guard could not safely reserve this request") from error
        else:
            async with self._memory_lock:
                self._prune_memory_locked()
                now = self.clock()
                existing = self._memory_idempotency.get(idem_key)
                if existing and existing[1] > now:
                    raise DuplicateChatRequest(existing[0] != fingerprint)
                self._memory_idempotency[idem_key] = (fingerprint, now + self.idempotency_ttl)
                active = self._memory_locks.get(lock_key)
                if active and active[1] > now:
                    self._memory_idempotency.pop(idem_key, None)
                    raise ChatConcurrentRequest()
                self._memory_locks[lock_key] = (nonce, now + self.concurrency_lease_seconds)
                for item in counters:
                    current, expires = self._memory_counters.get(item.key, (0, now + item.ttl))
                    if expires <= now:
                        current, expires = 0, now + item.ttl
                    if current + item.amount > item.limit:
                        self._memory_locks.pop(lock_key, None)
                        self._memory_idempotency.pop(idem_key, None)
                        raise ChatRateLimitExceeded(item.scope, math.ceil(expires - now))
                for item in counters:
                    current, expires = self._memory_counters.get(item.key, (0, now + item.ttl))
                    if expires <= now:
                        current, expires = 0, now + item.ttl
                    self._memory_counters[item.key] = (current + item.amount, expires)
                self._warn_memory_budgets(counters)

        ticket = ChatGuardTicket(lock_key=lock_key, lock_nonce=nonce, backend=backend)
        ticket.renew_task = asyncio.create_task(self._renew(ticket))
        return ticket

    async def check_conversation_create(self, *, user_id: str, ip: str) -> None:
        """Atomically reserve one new-conversation action for an account and IP."""
        backend, client = await self._backend()
        user_hash = self._hash(user_id)
        ip_hash = self._hash(ip)
        counters: list[_Counter] = []
        for limit, seconds in self.conversation_create_account_limits:
            counters.append(
                self._window_counter(
                    "conversation_create_account", user_hash, seconds, 1, limit
                )
            )
        for limit, seconds in self.conversation_create_ip_limits:
            counters.append(
                self._window_counter(
                    "conversation_create_ip", ip_hash, seconds, 1, limit
                )
            )

        if backend == "redis":
            assert client is not None
            argv: list[int] = []
            for item in counters:
                argv.extend((item.amount, item.limit, item.ttl))
            try:
                result = await client.eval(
                    _COUNTER_SCRIPT,
                    len(counters),
                    *(item.key for item in counters),
                    *argv,
                )
                if int(result[0]) != 1:
                    failed_index = max(0, int(result[1]) - 1)
                    raise ChatRateLimitExceeded(
                        counters[failed_index].scope, int(result[2])
                    )
            except ChatGuardError:
                raise
            except Exception as error:
                raise ChatGuardUnavailable(
                    "conversation creation guard could not safely reserve this request"
                ) from error
            return

        async with self._memory_lock:
            self._prune_memory_locked()
            now = self.clock()
            for item in counters:
                current, expires = self._memory_counters.get(
                    item.key, (0, now + item.ttl)
                )
                if expires <= now:
                    current, expires = 0, now + item.ttl
                if current + item.amount > item.limit:
                    raise ChatRateLimitExceeded(
                        item.scope, max(1, math.ceil(expires - now))
                    )
            for item in counters:
                current, expires = self._memory_counters.get(
                    item.key, (0, now + item.ttl)
                )
                if expires <= now:
                    current, expires = 0, now + item.ttl
                self._memory_counters[item.key] = (current + item.amount, expires)

    async def finish(self, ticket: Optional[ChatGuardTicket]) -> None:
        if ticket is None:
            return
        task = ticket.renew_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if ticket.backend == "redis" and self._redis is not None:
            try:
                await self._redis.eval(_COMPARE_DELETE_SCRIPT, 1, ticket.lock_key, ticket.lock_nonce)
            except Exception:
                return
        else:
            async with self._memory_lock:
                active = self._memory_locks.get(ticket.lock_key)
                if active and hmac.compare_digest(active[0], ticket.lock_nonce):
                    self._memory_locks.pop(ticket.lock_key, None)

    async def _renew(self, ticket: ChatGuardTicket) -> None:
        interval = max(5, self.concurrency_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            if ticket.backend == "redis" and self._redis is not None:
                renewed = await self._redis.eval(
                    _COMPARE_EXPIRE_SCRIPT,
                    1,
                    ticket.lock_key,
                    ticket.lock_nonce,
                    self.concurrency_lease_seconds,
                )
                if not renewed:
                    return
            else:
                async with self._memory_lock:
                    active = self._memory_locks.get(ticket.lock_key)
                    if not active or not hmac.compare_digest(active[0], ticket.lock_nonce):
                        return
                    self._memory_locks[ticket.lock_key] = (
                        ticket.lock_nonce,
                        self.clock() + self.concurrency_lease_seconds,
                    )

    def _prune_memory_locked(self) -> None:
        now = self.clock()
        self._memory_counters = {key: value for key, value in self._memory_counters.items() if value[1] > now}
        self._memory_idempotency = {key: value for key, value in self._memory_idempotency.items() if value[1] > now}
        self._memory_locks = {key: value for key, value in self._memory_locks.items() if value[1] > now}
        self._memory_warned = {key: expires for key, expires in self._memory_warned.items() if expires > now}
        total = len(self._memory_counters) + len(self._memory_idempotency) + len(self._memory_locks)
        if total > self.memory_max_entries:
            # Fail closed instead of letting attacker-controlled keys exhaust RAM.
            raise ChatGuardUnavailable("local chat guard capacity reached")

    @staticmethod
    def _is_global_budget(scope: str) -> bool:
        return scope in {"global_requests", "global_tokens", "global_cost"}

    async def _warn_redis_budgets(self, client, counters: Iterable[_Counter]) -> None:
        for item in counters:
            if not self._is_global_budget(item.scope):
                continue
            current = int(await client.get(item.key) or 0)
            if current * 100 < item.limit * self.warning_percent:
                continue
            warning_key = f"{item.key}:warning:{self.warning_percent}"
            first_warning = await client.set(warning_key, "1", nx=True, ex=item.ttl)
            if first_warning:
                _LOGGER.warning(
                    "chat_budget_warning scope=%s usage_percent=%s",
                    item.scope,
                    min(100, math.ceil(current * 100 / item.limit)),
                )

    def _warn_memory_budgets(self, counters: Iterable[_Counter]) -> None:
        now = self.clock()
        for item in counters:
            if not self._is_global_budget(item.scope):
                continue
            current, expires = self._memory_counters.get(item.key, (0, now))
            if current * 100 < item.limit * self.warning_percent:
                continue
            warning_key = f"{item.key}:warning:{self.warning_percent}"
            if self._memory_warned.get(warning_key, 0) > now:
                continue
            self._memory_warned[warning_key] = expires
            _LOGGER.warning(
                "chat_budget_warning scope=%s usage_percent=%s",
                item.scope,
                min(100, math.ceil(current * 100 / item.limit)),
            )


class GuestSigner:
    """Issue and verify opaque guest identity cookies."""

    VERSION = "v1"

    def __init__(
        self,
        *,
        secret: str,
        ttl_seconds: int = 30 * 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret or "") < 24:
            raise ValueError("guest session secret must contain at least 24 characters")
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = max(300, int(ttl_seconds))
        self.clock = clock

    def issue(self, guest_id: Optional[str] = None) -> tuple[str, str]:
        guest_id = guest_id or f"guest-{uuid4()}"
        self._validate_guest_id(guest_id)
        issued_at = int(self.clock())
        expires_at = issued_at + self.ttl_seconds
        unsigned = f"{self.VERSION}.{guest_id}.{issued_at}.{expires_at}"
        signature = hmac.new(self.secret, unsigned.encode("utf-8"), hashlib.sha256).hexdigest()
        return guest_id, f"{unsigned}.{signature}"

    def verify(self, token: str) -> Optional[str]:
        try:
            version, guest_id, issued_raw, expires_raw, signature = token.split(".", 4)
            if version != self.VERSION:
                return None
            self._validate_guest_id(guest_id)
            issued_at = int(issued_raw)
            expires_at = int(expires_raw)
        except (AttributeError, TypeError, ValueError):
            return None
        now = int(self.clock())
        if issued_at > now + 60 or expires_at < now or expires_at - issued_at > self.ttl_seconds + 60:
            return None
        unsigned = f"{version}.{guest_id}.{issued_at}.{expires_at}"
        expected = hmac.new(self.secret, unsigned.encode("utf-8"), hashlib.sha256).hexdigest()
        return guest_id if hmac.compare_digest(expected, signature) else None

    @staticmethod
    def _validate_guest_id(value: str) -> None:
        if not value.startswith("guest-"):
            raise ValueError("invalid guest id")
        parsed = UUID(value[6:])
        if parsed.version != 4 or value != f"guest-{parsed}":
            raise ValueError("invalid guest id")


def hash_payload(message: str, flow: Optional[str]) -> str:
    return hashlib.sha256(f"{flow or ''}\n{message}".encode("utf-8")).hexdigest()
