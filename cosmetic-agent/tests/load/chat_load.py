#!/usr/bin/env python3
"""Authenticated BLINK staging streaming load probe.

Creates one isolated guest session per virtual user, records time-to-first-byte,
total latency, HTTP/SSE failures, and writes an optional JSON report.
The probe refuses managed production targets and verifies APP_RELEASE before load.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:18110"
PUBLIC_STAGING_HOST = "staging-api.blinkai.tw"
PRODUCTION_HOSTS = {"api.blinkai.tw"}
PRODUCTION_PORTS = {8000, 8001, 8002, 8003, 8010, 8200}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

MESSAGES = (
    "幫我找一千塊以下的精華液",
    "推薦敏感肌適用、無香料的防曬乳",
    "油肌夏天適合什麼清爽乳液？",
    "乾肌保濕面膜兩千元以下",
    "我該先擦精華還是乳霜？",
)


def validate_target(base_url: str) -> str:
    target = base_url.rstrip("/")
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target must be an absolute HTTP(S) URL")
    hostname = parsed.hostname.lower()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("target has an invalid port") from exc
    if hostname in PRODUCTION_HOSTS:
        raise ValueError("refusing to load-test the production API host")
    if port == 8010:
        raise ValueError("refusing to load-test production port 8010")
    if hostname in LOOPBACK_HOSTS and port in PRODUCTION_PORTS:
        raise ValueError("refusing to load-test a managed production port")
    if hostname == PUBLIC_STAGING_HOST and parsed.scheme != "https":
        raise ValueError("public staging target must use HTTPS")
    return target


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


def issue_local_guest() -> tuple[str, str]:
    """Issue a valid test identity without consuming the public auth rate limit."""
    from api.api_server import _guest_signer

    return _guest_signer().issue()


async def verify_release(base_url: str, expected_release: str, timeout: float) -> None:
    """Abort before fan-out unless readiness reports the exact staging release."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(min(timeout, 10.0))) as client:
        response = await client.get(base_url + "/readyz")
        response.raise_for_status()
        if response.headers.get("X-App-Release") != expected_release:
            raise RuntimeError("release header mismatch on readiness preflight")


async def run_user(index: int, args: argparse.Namespace) -> dict:
    if args.stagger_ms:
        await asyncio.sleep(index * args.stagger_ms / 1000)
    result = {
        "user": index,
        "status": 0,
        "success": False,
        "ttfb_ms": None,
        "total_ms": None,
        "bytes": 0,
        "error": "",
    }
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=2)
    timeout = httpx.Timeout(args.timeout)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            if args.local_signed_guests:
                user_id, token = issue_local_guest()
                client.cookies.set("blink_guest_session", token)
            else:
                auth = await client.post(args.base_url + "/api/auth/guest-session")
                auth.raise_for_status()
                user_id = auth.json()["user_id"]
            payload = {
                "user_id": user_id,
                "conversation_id": "load-" + str(uuid.uuid4()),
                "message": MESSAGES[index % len(MESSAGES)],
                "flow": "auto",
            }
            first_byte_at = None
            chunks: list[bytes] = []
            headers = {"Idempotency-Key": str(uuid.uuid4())}
            async with client.stream(
                "POST",
                args.base_url + "/api/chat/message/stream",
                json=payload,
                headers=headers,
            ) as response:
                result["status"] = response.status_code
                if response.headers.get("X-App-Release") != args.expected_release:
                    raise RuntimeError("release header mismatch on stream")
                server_timing = response.headers.get("server-timing", "")
                for item in server_timing.split(","):
                    metric, _, value = item.strip().partition(";dur=")
                    if metric == "queue" and value:
                        try:
                            result["queue_ms"] = round(float(value), 3)
                        except ValueError:
                            pass
                async for chunk in response.aiter_bytes():
                    if chunk and first_byte_at is None:
                        first_byte_at = time.perf_counter()
                    if chunk:
                        chunks.append(chunk)
            ended = time.perf_counter()
            body = b"".join(chunks).decode("utf-8", errors="replace")
            result["bytes"] = len(body.encode("utf-8"))
            result["ttfb_ms"] = round(
                ((first_byte_at or ended) - started) * 1000,
                3,
            )
            result["total_ms"] = round((ended - started) * 1000, 3)
            has_sse_error = "event: error" in body
            result["success"] = (
                result["status"] == 200 and bool(body.strip()) and not has_sse_error
            )
            if has_sse_error:
                result["error"] = "sse_error"
            elif not body.strip():
                result["error"] = "empty_stream"
    except Exception as exc:
        result["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        result["error"] = type(exc).__name__ + ": " + str(exc)[:200]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--users", type=int, default=30)
    parser.add_argument("--stagger-ms", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--json", dest="json_path")
    parser.add_argument(
        "--local-signed-guests",
        action="store_true",
        help="Create unique local guest identities without hitting the auth issue limiter",
    )
    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.base_url = validate_target(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.users <= 200:
        parser.error("--users must be between 1 and 200")

    try:
        await verify_release(args.base_url, args.expected_release, args.timeout)
    except Exception as exc:
        report = {
            "base_url": args.base_url,
            "release": args.expected_release,
            "verdict": "ABORTED",
            "error": type(exc).__name__ + ": " + str(exc)[:200],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    wall_started = time.perf_counter()
    results = await asyncio.gather(*(run_user(i, args) for i in range(args.users)))
    wall_ms = round((time.perf_counter() - wall_started) * 1000, 3)
    latencies = [r["total_ms"] for r in results if r["total_ms"] is not None]
    ttfb = [r["ttfb_ms"] for r in results if r["ttfb_ms"] is not None]
    queue_wait = [r["queue_ms"] for r in results if r.get("queue_ms") is not None]
    succeeded = sum(bool(r["success"]) for r in results)
    summary = {
        "base_url": args.base_url,
        "release": args.expected_release,
        "users": args.users,
        "succeeded": succeeded,
        "failed": args.users - succeeded,
        "success_rate": round(succeeded / args.users, 4),
        "wall_ms": wall_ms,
        "total_ms": {
            "min": round(min(latencies), 3) if latencies else None,
            "median": round(statistics.median(latencies), 3) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "ttfb_ms": {
            "median": round(statistics.median(ttfb), 3) if ttfb else None,
            "p95": percentile(ttfb, 0.95),
        },
        "queue_wait_ms": {
            "median": round(statistics.median(queue_wait), 3) if queue_wait else None,
            "p95": percentile(queue_wait, 0.95),
        },
        "metrics_to_verify": [
            "blink_event_loop_lag_seconds",
            "blink_pipeline_stage_duration_seconds{pipeline=chat,stage=first_token}",
            "blink_pipeline_stage_duration_seconds{pipeline=chat,stage=cancel_release}",
        ],
        "statuses": dict(Counter(str(r["status"]) for r in results)),
        "errors": dict(Counter(r["error"] for r in results if r["error"])),
        "results": results,
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_path:
        Path(args.json_path).write_text(rendered, encoding="utf-8")
    return 0 if succeeded == args.users else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
