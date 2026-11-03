#!/usr/bin/env python3
"""Fail-fast validation for the backend Redis asyncio and OpenTelemetry runtime."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import os
import sys

REQUIRED_OTEL_MODULES = (
    "opentelemetry.sdk",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.httpx",
    "opentelemetry.instrumentation.requests",
    "opentelemetry.instrumentation.redis",
    "opentelemetry.instrumentation.psycopg",
)


def _check_packages() -> str:
    redis_version = importlib.metadata.version("redis")
    try:
        redis_major = int(redis_version.split(".", 1)[0])
    except ValueError as error:
        raise RuntimeError(f"Unrecognized redis-py version: {redis_version}") from error
    if redis_major < 5:
        raise RuntimeError(
            f"redis-py >= 5 is required for the async state stores; found {redis_version}"
        )

    importlib.import_module("redis.asyncio")
    for module_name in REQUIRED_OTEL_MODULES:
        importlib.import_module(module_name)
    return redis_version


async def _check_redis(redis_url: str) -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    try:
        if not await client.ping():
            raise RuntimeError("Redis PING returned a false value")
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
        else:
            await client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--packages-only",
        action="store_true",
        help="Validate installed packages without connecting to Redis.",
    )
    parser.add_argument(
        "--redis-url-env",
        default="REDIS_URL",
        help="Name of the environment variable containing the Redis URL.",
    )
    args = parser.parse_args()

    redis_version = _check_packages()
    print(f"[OK] redis-py {redis_version} with redis.asyncio")
    print(f"[OK] OpenTelemetry modules: {len(REQUIRED_OTEL_MODULES)}")

    if not args.packages_only:
        redis_url = os.getenv(args.redis_url_env, "").strip()
        if not redis_url:
            raise RuntimeError(f"{args.redis_url_env} is required")
        asyncio.run(_check_redis(redis_url))
        print("[OK] Redis async PING")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[ERROR] backend runtime preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)
