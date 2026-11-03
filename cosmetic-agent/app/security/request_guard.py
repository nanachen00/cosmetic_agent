"""Request guards for API body sizes and shared rate limits.

Redis is used when configured so every worker and replica shares the same
counters.  The bounded in-process limiter remains available for tests and for
explicitly non-production environments.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable


@dataclass(frozen=True)
class RateLimitRule:
    requests: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.requests < 1 or self.window_seconds < 1:
            raise ValueError("Rate limits must be positive")


class RateLimiterUnavailable(RuntimeError):
    """Raised when a required shared limiter cannot be reached."""


class FixedWindowLimiter:
    """A bounded per-process limiter with deterministic retry information."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
    ) -> None:
        self._clock = clock
        self._max_buckets = max(100, max_buckets)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = Lock()

    def check(self, key: str, rule: RateLimitRule) -> int:
        """Record one request; return zero or Retry-After seconds when denied."""
        now = self._clock()
        with self._lock:
            if len(self._buckets) >= self._max_buckets:
                self._discard_expired(now, rule.window_seconds)

            started_at, count = self._buckets.get(key, (now, 0))
            elapsed = now - started_at
            if elapsed >= rule.window_seconds:
                started_at, count = now, 0

            if count >= rule.requests:
                return max(1, math.ceil(rule.window_seconds - elapsed))

            self._buckets[key] = (started_at, count + 1)
            return 0

    def _discard_expired(self, now: float, window_seconds: int) -> None:
        expired = [
            key
            for key, (started_at, _) in self._buckets.items()
            if now - started_at >= window_seconds
        ]
        for key in expired:
            self._buckets.pop(key, None)

        while len(self._buckets) >= self._max_buckets:
            self._buckets.pop(next(iter(self._buckets)))


class RedisFixedWindowLimiter:
    """A fixed-window limiter shared by all workers through Redis."""

    _INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        require_redis: bool | None = None,
        key_prefix: str = "blink:api-rate:v1",
        clock: Callable[[], float] = time.time,
        fallback: FixedWindowLimiter | None = None,
    ) -> None:
        configured_url = redis_url if redis_url is not None else os.getenv("REDIS_URL", "")
        self._redis_url = configured_url.strip()
        if require_redis is None:
            raw_required = os.getenv(
                "API_LIMIT_REQUIRE_REDIS",
                os.getenv("CHAT_LIMIT_REQUIRE_REDIS", "0"),
            )
            require_redis = raw_required.strip().lower() in {"1", "true", "yes", "on"}
        self._require_redis = require_redis
        self._key_prefix = key_prefix.strip(":")
        self._clock = clock
        self._fallback = fallback or FixedWindowLimiter()
        self._client = None

    async def check(self, key: str, rule: RateLimitRule) -> int:
        """Record one request and return zero or Retry-After seconds."""
        if not self._redis_url:
            if self._require_redis:
                raise RateLimiterUnavailable("REDIS_URL is required for API rate limiting")
            return self._fallback.check(key, rule)

        try:
            if self._client is None:
                from redis.asyncio import Redis

                self._client = Redis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )

            now = float(self._clock())
            bucket = int(now // rule.window_seconds)
            retry_after = max(
                1,
                math.ceil(((bucket + 1) * rule.window_seconds) - now),
            )
            identity_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            redis_key = (
                f"{self._key_prefix}:{rule.window_seconds}:{bucket}:{identity_hash}"
            )
            count = int(
                await self._client.eval(
                    self._INCREMENT_SCRIPT,
                    1,
                    redis_key,
                    retry_after,
                )
            )
            return retry_after if count > rule.requests else 0
        except RateLimiterUnavailable:
            raise
        except Exception as exc:
            if self._require_redis:
                raise RateLimiterUnavailable(
                    "Shared API rate limiter is unavailable"
                ) from exc
            return self._fallback.check(key, rule)
