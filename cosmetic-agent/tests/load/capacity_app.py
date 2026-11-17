"""Isolated capacity fixture using production routes, guards, admission and jobs.

Only the agent and history/profile IO are replaced. No API key is required.
Never mount this app in the production ASGI composition root.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from redis import Redis
from app.api.routes import chat as chat_routes
from app.api.routes.chat import ChatRouteDependencies, create_chat_router
from app.security.chat_guard import (
    ChatGuard, ChatGuardError, ChatGuardUnavailable, DuplicateChatRequest, GuestSigner, hash_payload,
)
from app.services.chat_job_store import RedisChatJobStore
from core.admission import AdmissionFull, AdmissionTimeout, RedisChatAdmissionController
from core.conversation_state import ConversationStateStore, shared_conversation_state

if os.getenv("BLINK_CAPACITY_TEST") != "1":
    raise RuntimeError("Capacity fixture requires BLINK_CAPACITY_TEST=1")

URL = os.environ["CAPACITY_REDIS_URL"]
PREFIX = os.environ["CAPACITY_PREFIX"]
RELEASE = os.environ["APP_RELEASE"]
signer = GuestSigner(secret=os.environ["CAPACITY_SECRET"])
sync_redis = Redis.from_url(URL, decode_responses=True)
admission = RedisChatAdmissionController(URL, max_running=8, max_queued=32, key_prefix=PREFIX + ":admission")
jobs = RedisChatJobStore(URL, key_prefix=PREFIX + ":jobs")
chat_routes.chat_job_store = jobs
guard = ChatGuard(
    redis_url=URL, require_redis=True, key_prefix=PREFIX + ":guard",
    account_limits=((10000, 60),), guest_limits=((10000, 60),),
    ip_limits=((10000, 60),), global_daily_requests=100000,
    global_daily_tokens=100000000,
)


class MockState:
    def __init__(self):
        self.turn_count = 0
        self.is_waiting_confirm = False


class MockAgent:
    def __init__(self, user_id, conversation_id):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.state = MockState()
        self._conversation_state_store = ConversationStateStore(sync_redis, prefix=PREFIX + ":state")

    @shared_conversation_state
    def process(self, message, *, on_delta=None, **kwargs):
        active = sync_redis.incr(PREFIX + ":inference:active")
        sync_redis.eval(
            "local v=tonumber(redis.call('GET',KEYS[1]) or '0'); "
            "if tonumber(ARGV[1])>v then redis.call('SET',KEYS[1],ARGV[1]) end; return 1",
            1, PREFIX + ":inference:peak", active,
        )
        sync_redis.incr(PREFIX + ":inference:total")
        try:
            time.sleep(float(os.getenv("CAPACITY_MODEL_DELAY", "0.08")))
            if on_delta:
                on_delta("測試回覆")
            self.state.turn_count += 1
            self.state.is_waiting_confirm = True
            return {"reply": "測試回覆", "stage": "complete", "worker_pid": os.getpid(), "turn_count": self.state.turn_count}
        finally:
            sync_redis.decr(PREFIX + ":inference:active")


def user_id(request: Request, requested=None):
    verified = signer.verify(request.cookies.get("blink_guest_session", ""))
    if not verified or requested not in {None, verified}:
        raise HTTPException(401, "invalid test identity")
    return verified


async def begin(request, req, user):
    try:
        return await guard.begin(
            user_id=user, ip="127.0.0.1", request_id=request.headers.get("Idempotency-Key", ""),
            route=request.url.path, conversation_id=req.cid,
            payload_hash=hash_payload(req.message, req.flow), is_guest=True, input_tokens=10,
        )
    except ChatGuardError as exc:
        status = 503 if isinstance(exc, ChatGuardUnavailable) else 409 if isinstance(exc, DuplicateChatRequest) else 429
        raise HTTPException(status, str(exc)) from exc


async def reserve():
    try:
        return await admission.reserve()
    except AdmissionFull as exc:
        raise HTTPException(503, "test queue full") from exc


async def wait(ticket):
    try:
        return await ticket.wait(5)
    except AdmissionTimeout as exc:
        raise HTTPException(503, "test queue timeout") from exc


async def history(**kwargs):
    return ["test history"]


async def no_creation(*args, **kwargs):
    return None


@asynccontextmanager
async def lifespan(app):
    yield
    await admission._redis.aclose()
    await jobs._client.aclose()
    if guard._redis is not None:
        await guard._redis.aclose()
    sync_redis.close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def provenance(request, call_next):
    response = await call_next(request)
    response.headers["X-App-Release"] = RELEASE
    response.headers["X-Worker-Pid"] = str(os.getpid())
    return response


@app.get("/readyz")
def ready():
    sync_redis.ping()
    return {"status": "ready", "release": RELEASE, "scope": "production-chat-route-mock-agent"}


app.include_router(create_chat_router(ChatRouteDependencies(
    chat_user_id=user_id, begin_chat_security=begin,
    get_agent_with_profile=lambda **kwargs: MockAgent(**kwargs), enforce_conversation_limit=history,
    guard_conversation_creation=no_creation, reserve_chat_ticket=reserve,
    wait_for_chat_ticket=wait, completed_conversation_limit=lambda *args: {},
    finish_chat_security=guard.finish, stage_to_route=lambda stage: stage,
    get_agent=lambda **kwargs: MockAgent(**kwargs),
)))
