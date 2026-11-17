#!/usr/bin/env python3
"""Run 1/2/3-worker production chat-route tests against private Redis + mock IO.

No .env is sourced and no provider API is called. Results are not model capacity.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from redis import Redis

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.security.chat_guard import GuestSigner
from tests.load.chat_load import percentile


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, *, env, cwd, log):
    with log.open("w") as output:
        child = subprocess.Popen(command, env=env, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            yield child
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)


async def wait_ready(base, release, child):
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError("test process exited during startup; inspect log")
            try:
                response = await client.get(base + "/readyz")
                if response.status_code == 200 and response.headers.get("X-App-Release") == release:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("test app readiness timeout")


def decode_frames(body):
    events = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in frame.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.lstrip()
        if "event" in fields:
            fields["data"] = json.loads(fields.get("data", "{}"))
            events.append(fields)
    return events


async def request(base, signer, release, *, identity=None, request_id=None, last_event_id=None):
    user, token = identity or signer.issue()
    request_id = request_id or str(uuid.uuid4())
    headers = {"Idempotency-Key": request_id}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    started = time.perf_counter()
    first_delta = None
    buffer = ""
    async with httpx.AsyncClient(timeout=15, cookies={"blink_guest_session": token}) as client:
        async with client.stream("POST", base + "/api/chat/message/stream", headers=headers, json={
            "user_id": user, "conversation_id": "capacity-conversation",
            "message": "保濕建議", "flow": "skin_consult",
        }) as response:
            async for chunk in response.aiter_text():
                buffer += chunk
                if first_delta is None and "event: delta" in buffer:
                    first_delta = (time.perf_counter() - started) * 1000
            events = decode_frames(buffer) if response.status_code == 200 else []
            result = {
                "http": response.status_code,
                "worker": response.headers.get("X-Worker-Pid"),
                "release_match": response.headers.get("X-App-Release") == release,
                "terminal": events[-1]["event"] if events else None,
                "turn_count": events[-1]["data"].get("turn_count") if events else None,
                "first_delta_ms": first_delta,
                "total_ms": (time.perf_counter() - started) * 1000,
                "queue_ms": next((event["data"]["queue_ms"] for event in events if "queue_ms" in event["data"]), 0),
            }
            return result


async def verify_replay_and_cleanup(base, signer, release, redis, prefix):
    identity = signer.issue()
    rid = str(uuid.uuid4())
    before = int(redis.get(prefix + ":inference:total") or 0)
    first = await request(base, signer, release, identity=identity, request_id=rid)
    assert first["terminal"] == "done", first
    duplicate = await request(base, signer, release, identity=identity, request_id=rid)
    assert duplicate["http"] == 409, duplicate
    workers = set()
    for _ in range(3):
        replays = await asyncio.gather(*(request(base, signer, release, identity=identity, request_id=rid, last_event_id=0) for _ in range(20)))
        for replay in replays:
            assert replay["terminal"] == "done", replay
            workers.add(replay["worker"])
    assert int(redis.get(prefix + ":inference:total")) == before + 1
    for turn in range(2, 7):
        following = await request(base, signer, release, identity=identity)
        assert following["turn_count"] == turn, following

    # Disconnect once generation starts; the producer must finish and release.
    user, token = signer.issue()
    async with httpx.AsyncClient(timeout=10, cookies={"blink_guest_session": token}) as client:
        async with client.stream("POST", base + "/api/chat/message/stream", headers={"Idempotency-Key": str(uuid.uuid4())}, json={
            "user_id": user, "conversation_id": "disconnect", "message": "保濕", "flow": "skin_consult",
        }) as response:
            async for chunk in response.aiter_text():
                if "processing" in chunk:
                    break
    await asyncio.sleep(0.4)
    assert redis.zcard(prefix + ":admission:active") == 0
    assert redis.zcard(prefix + ":admission:queue") == 0
    assert not list(redis.scan_iter(prefix + ":guard:active:*"))
    return {"duplicate_http": 409, "replay_workers": sorted(workers), "model_calls_for_replayed_job": 1, "shared_dialogue_turns": 6, "disconnect_cleanup": "PASS"}


async def run(args):
    server = shutil.which("redis-server")
    if not server:
        raise RuntimeError("Install Redis first: brew install redis")
    run_id = uuid.uuid4().hex
    redis_port = unused_port()
    url = f"redis://127.0.0.1:{redis_port}/0"
    secret = secrets.token_hex(32)
    signer = GuestSigner(secret=secret)
    # Explicit environment, with no inherited API keys or service credentials.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT") if key in os.environ}
    env.update({"BLINK_CAPACITY_TEST": "1", "CAPACITY_REDIS_URL": url,
                "CAPACITY_SECRET": secret, "PYTHONPATH": str(ROOT),
                "TEMPORAL_FRESHNESS_GUARD_ENABLED": "0", "PYTHONUNBUFFERED": "1", "PYTHON_DOTENV_DISABLED": "1"})
    report = {"scope": "production-chat-route-mock-agent", "run_id": run_id,
              "timestamp": datetime.now(timezone.utc).isoformat(), "model_api_calls": 0,
              "limitations": ["Agent/history/profile IO mocked", "No browser, external DB, Terra, or GPU capacity claim", "Short burst test, not a long-duration soak"], "workers": []}
    with tempfile.TemporaryDirectory(prefix="blink-capacity-") as directory:
        scratch = Path(directory)
        with process([server, "--bind", "127.0.0.1", "--port", str(redis_port), "--save", "", "--appendonly", "no", "--dir", directory], env=env, cwd=ROOT, log=scratch / "redis.log"):
            redis = Redis.from_url(url, decode_responses=True)
            try:
                for _ in range(50):
                    try:
                        redis.ping()
                        break
                    except Exception:
                        await asyncio.sleep(0.1)
                for count in args.workers:
                    prefix = f"capacity:{run_id}:w{count}"
                    release = f"capacity-{run_id}-w{count}"
                    port = unused_port()
                    base = f"http://127.0.0.1:{port}"
                    child_env = {**env, "CAPACITY_PREFIX": prefix, "APP_RELEASE": release}
                    log_path = args.output.parent / f"capacity-w{count}.log"
                    with process([sys.executable, "-m", "uvicorn", "tests.load.capacity_app:app", "--host", "127.0.0.1", "--port", str(port), "--workers", str(count), "--no-access-log"], env=child_env, cwd=ROOT, log=log_path) as child:
                        await wait_ready(base, release, child)
                        groups = []
                        for concurrency in (1, 4, 8, 16, 32):
                            rows = []
                            for _ in range(args.rounds):
                                rows.extend(await asyncio.gather(*(request(base, signer, release) for _ in range(concurrency))))
                            assert all(r["http"] == 200 and r["terminal"] == "done" and r["release_match"] for r in rows), rows
                            groups.append({"concurrency": concurrency, "requests": len(rows), "success": len(rows),
                                           "p50_ms": percentile([r["total_ms"] for r in rows], .5),
                                           "p95_ms": percentile([r["total_ms"] for r in rows], .95),
                                           "first_delta_p95_ms": percentile([r["first_delta_ms"] for r in rows if r["first_delta_ms"] is not None], .95),
                                           "queue_p95_ms": percentile([r["queue_ms"] for r in rows], .95),
                                           "worker_pids": sorted({r["worker"] for r in rows})})
                        extra = await verify_replay_and_cleanup(base, signer, release, redis, prefix)
                        if count > 1:
                            assert len(extra["replay_workers"]) > 1, "cross-worker replay was not exercised"
                        peak = int(redis.get(prefix + ":inference:peak") or 0)
                        assert 0 < peak <= 8, peak
                        assert redis.zcard(prefix + ":admission:active") == 0
                        assert redis.zcard(prefix + ":admission:queue") == 0
                        overloaded = await asyncio.gather(*(request(base, signer, release) for _ in range(96)))
                        rejected = sum(r["http"] == 503 for r in overloaded)
                        assert rejected > 0, "queue-full rejection was not exercised"
                        assert all(r["http"] == 503 or (r["http"] == 200 and r["terminal"] == "done") for r in overloaded)
                        await asyncio.sleep(0.2)
                        assert redis.zcard(prefix + ":admission:active") == 0
                        assert redis.zcard(prefix + ":admission:queue") == 0
                        assert not list(redis.scan_iter(prefix + ":guard:active:*"))
                        extra["overload"] = {"requests": 96, "rejected_503": rejected, "cleanup": "PASS"}
                        redis.zadd(prefix + ":admission:queue", {"dead-worker-ticket": time.time() - 3600})
                        recovered = await request(base, signer, release)
                        assert recovered["terminal"] == "done", recovered
                        assert redis.zcard(prefix + ":admission:queue") == 0
                        extra["dead_worker_queue_recovery"] = "PASS"
                        if count == args.workers[-1]:
                            redis.shutdown(nosave=True)
                            outage = await request(base, signer, release)
                            assert outage["http"] == 503, outage
                            extra["redis_outage_fail_closed"] = "PASS"
                        item = {"workers": count, "groups": groups, "global_peak_running": peak, "checks": extra}
                        report["workers"].append(item)
                        print(f"[PASS] workers={count} requests={sum(g['requests'] for g in groups)} peak={peak} cross-worker replay/cleanup OK", flush=True)
            finally:
                redis.close()
    report["verdict"] = "PASS"
    report["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "app/api/routes/chat.py", ROOT / "core/admission.py", Path(__file__), Path(__file__).with_name("capacity_app.py"))}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report={args.output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", nargs="+", type=int, choices=(1, 2, 3), default=[1, 2, 3])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT.parent / ".runtime/reports/capacity.json")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("rounds must be between 1 and 100")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        args.output.write_text(json.dumps({"verdict": "FAIL", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2) + "\n")
        raise


if __name__ == "__main__":
    main()
