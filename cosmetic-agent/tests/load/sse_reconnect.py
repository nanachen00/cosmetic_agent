#!/usr/bin/env python3
"""Exercise SSE disconnect/reconnect using Idempotency-Key and Last-Event-ID."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCTION_PORTS = {8000, 8001, 8002, 8003, 8010, 8200}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "staging-api.blinkai.tw"}


def validate_target(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("target must be loopback or staging-api.blinkai.tw")
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and (parsed.port or 80) in PRODUCTION_PORTS:
        raise ValueError("refusing to probe a managed production port")
    if parsed.hostname == "staging-api.blinkai.tw" and parsed.scheme != "https":
        raise ValueError("public staging target must use HTTPS")
    return base_url.rstrip("/")


def decode_sse(lines: list[str]) -> dict[str, object] | None:
    if not lines or all(not line or line.startswith(":") for line in lines):
        return None
    event_id = ""
    event = "message"
    data: list[str] = []
    for line in lines:
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event = value
        elif field == "data":
            data.append(value)
    raw_data = "\n".join(data)
    try:
        payload: object = json.loads(raw_data) if raw_data else None
    except json.JSONDecodeError:
        payload = raw_data
    return {"id": event_id, "event": event, "data": payload}


async def iter_sse(response: httpx.Response):
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            decoded = decode_sse(lines)
            lines = []
            if decoded is not None:
                yield decoded
        else:
            lines.append(line)
    decoded = decode_sse(lines)
    if decoded is not None:
        yield decoded


async def issue_guest(client: httpx.AsyncClient, base_url: str, local_signed: bool) -> str:
    if local_signed:
        from api.api_server import _guest_signer

        user_id, token = _guest_signer().issue()
        cookie_name = os.getenv("CHAT_GUEST_COOKIE_NAME", "blink_guest_session")
        client.cookies.set(cookie_name, token)
        return user_id
    response = await client.post(base_url + "/api/auth/guest-session")
    response.raise_for_status()
    return str(response.json()["user_id"])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18110")
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--disconnect-after-events", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--message", default="請用兩點簡短說明敏感肌選防曬的原則")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--local-signed-guest", action="store_true")
    args = parser.parse_args()
    try:
        base_url = validate_target(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.disconnect_after_events < 1:
        parser.error("--disconnect-after-events must be positive")

    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    first_events: list[dict[str, object]] = []
    resumed_events: list[dict[str, object]] = []
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        user_id = await issue_guest(client, base_url, args.local_signed_guest)
        payload = {
            "user_id": user_id,
            "conversation_id": "staging-reconnect-" + str(uuid.uuid4()),
            "message": args.message,
            "flow": "auto",
        }
        headers = {"Idempotency-Key": request_id}
        async with client.stream(
            "POST", base_url + "/api/chat/message/stream", json=payload, headers=headers
        ) as response:
            response.raise_for_status()
            if response.headers.get("X-App-Release") != args.expected_release:
                raise RuntimeError("release header mismatch on initial stream")
            async for event in iter_sse(response):
                first_events.append(event)
                if event.get("event") in {"done", "error"}:
                    raise RuntimeError("stream ended before the planned disconnect")
                identified = [item for item in first_events if item.get("id")]
                if len(identified) >= args.disconnect_after_events:
                    break

        first_ids = [str(item["id"]) for item in first_events if item.get("id")]
        if not first_ids:
            raise RuntimeError("initial stream did not provide an SSE event ID")
        last_event_id = first_ids[-1]
        reconnect_started = time.perf_counter()
        reconnect_headers = {
            "Idempotency-Key": request_id,
            "Last-Event-ID": last_event_id,
        }
        async with client.stream(
            "POST",
            base_url + "/api/chat/message/stream",
            json=payload,
            headers=reconnect_headers,
        ) as response:
            response.raise_for_status()
            if response.headers.get("X-App-Release") != args.expected_release:
                raise RuntimeError("release header mismatch on resumed stream")
            first_resumed_at = None
            async for event in iter_sse(response):
                if first_resumed_at is None:
                    first_resumed_at = time.perf_counter()
                resumed_events.append(event)
                if event.get("event") in {"done", "error"}:
                    break

        job_response = await client.get(
            base_url + "/api/chat/jobs/" + request_id,
            params={"user_id": user_id},
        )
        job_response.raise_for_status()
        job = job_response.json()

    resumed_ids = [str(item["id"]) for item in resumed_events if item.get("id")]
    all_ids = first_ids + resumed_ids
    terminal = [str(item.get("event")) for item in first_events + resumed_events if item.get("event") in {"done", "error"}]
    failures: list[str] = []
    if not resumed_events:
        failures.append("no events after reconnect")
    if set(first_ids).intersection(resumed_ids):
        failures.append("reconnect replayed an acknowledged event")
    if len(all_ids) != len(set(all_ids)):
        failures.append("duplicate SSE event IDs")
    if terminal != ["done"]:
        failures.append("terminal event was not exactly one done event")
    if job.get("status") != "completed":
        failures.append("job status is not completed")
    reconnect_ms = round(((first_resumed_at or time.perf_counter()) - reconnect_started) * 1000, 3)
    report = {
        "base_url": base_url,
        "release": args.expected_release,
        "request_id": request_id,
        "initial_event_count": len(first_events),
        "resumed_event_count": len(resumed_events),
        "unique_event_ids": len(all_ids) == len(set(all_ids)),
        "reconnect_ms": reconnect_ms,
        "terminal": terminal,
        "job_status": job.get("status"),
        "total_ms": round((time.perf_counter() - started) * 1000, 3),
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_path:
        Path(args.json_path).write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
