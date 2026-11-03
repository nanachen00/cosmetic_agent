from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.schemas.chat import ChatRequest, normalize_conversation_id
from app.services.chat_job_store import chat_job_store
from app.services.weather_context_service import prepare_freshness_context
from core.observability import observe_pipeline_stage


@dataclass(frozen=True)
class ChatRouteDependencies:
    chat_user_id: Any
    begin_chat_security: Any
    get_agent_with_profile: Any
    enforce_conversation_limit: Any
    guard_conversation_creation: Any
    reserve_chat_ticket: Any
    wait_for_chat_ticket: Any
    completed_conversation_limit: Any
    finish_chat_security: Any
    stage_to_route: Any
    get_agent: Any


async def _release_stream_resources_when_done(
    producer,
    ticket,
    guard_ticket,
    finish_chat_security,
    *,
    release_started_at: float,
) -> None:
    """Keep limits held while a disconnected client's sync model call finishes."""
    try:
        await producer
    except BaseException:
        pass
    finally:
        await ticket.release()
        await finish_chat_security(guard_ticket)
        observe_pipeline_stage("chat", "cancel_release", release_started_at)

def create_chat_router(deps: ChatRouteDependencies) -> APIRouter:
    router = APIRouter()

    async def _load_freshness_context(message: str):
        started = time.perf_counter()
        try:
            return await prepare_freshness_context(message)
        finally:
            observe_pipeline_stage("chat", "freshness_context", started)

    def _encode_sse(
        event: str, payload: dict, *, event_id: int | None = None
    ) -> str:
        id_line = f"id: {event_id}\n" if event_id is not None else ""
        return (
            f"{id_line}event: {event}\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )


    def _request_id(request: Request) -> str | None:
        value = request.headers.get("Idempotency-Key", "").strip()
        if not value:
            return None
        if len(value) > 128:
            raise HTTPException(status_code=400, detail="Invalid request id")
        return value

    def _last_event_id(request: Request) -> int:
        raw = request.headers.get("Last-Event-ID", "").strip()
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from error

    def _streaming_response(generator) -> StreamingResponse:
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def _replay_event_stream(
        request_id: str,
        *,
        user_id: str,
        last_event_id: int,
    ):
        cursor = last_event_id
        deadline = asyncio.get_running_loop().time() + 180.0
        while True:
            replay = await chat_job_store.events_after(
                request_id,
                user_id=user_id,
                last_event_id=cursor,
            )
            for item in replay:
                cursor = max(cursor, int(item["id"]))
                yield _encode_sse(
                    str(item["event"]),
                    dict(item["payload"]),
                    event_id=int(item["id"]),
                )
                if item["event"] in {"done", "error"}:
                    return

            job = await chat_job_store.get(request_id, user_id=user_id)
            if job is None:
                yield _encode_sse("error", {"message": "Chat request not found"})
                return
            status = job.get("status")
            if status in {"completed", "failed"}:
                event = "done" if status == "completed" else "error"
                payload = job.get("result") or {
                    "message": job.get("error") or "Chat failed"
                }
                event_id = await chat_job_store.append_event(
                    request_id,
                    user_id=user_id,
                    event=event,
                    payload=payload,
                )
                yield _encode_sse(event, payload, event_id=event_id)
                return
            if asyncio.get_running_loop().time() >= deadline:
                yield _encode_sse(
                    "error",
                    {"message": "Chat is still running", "retry_after": 5},
                )
                return
            await asyncio.sleep(0.5)

    @router.get("/api/chat/jobs/{request_id}")
    async def chat_job_status(
        request_id: str,
        request: Request,
        user_id: str | None = None,
    ):
        resolved_user_id = deps.chat_user_id(request, user_id)
        job = await chat_job_store.get(request_id, user_id=resolved_user_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Chat request not found")
        return job

    @router.post("/api/v1/chat")
    async def chat(
        req: ChatRequest,
        request: Request,
    ):
        user_id = deps.chat_user_id(request, req.uid)
        guard_ticket = await deps.begin_chat_security(request, req, user_id)
        ticket = None
        try:
            ticket = await deps.reserve_chat_ticket()
            await deps.wait_for_chat_ticket(ticket)
            agent = await run_in_threadpool(
                deps.get_agent_with_profile,
                user_id=user_id,
                conversation_id=req.cid,
            )
            history_for_limit = await deps.enforce_conversation_limit(
                agent=agent,
                request=request,
                user_id=user_id,
                incoming_message=req.message,
            )
            if not history_for_limit:
                await deps.guard_conversation_creation(request, user_id)
            freshness_context = await _load_freshness_context(req.message)
            result = await run_in_threadpool(
                agent.process,
                req.message,
                conversation_flow=req.flow,
                action=req.action,
                freshness_context=freshness_context,
            )
            if isinstance(result, dict):
                result["conversation_limit"] = deps.completed_conversation_limit(
                    history_for_limit, req.message, result
                )
            return result
        finally:
            if ticket is not None:
                await ticket.release()
            await deps.finish_chat_security(guard_ticket)


    @router.post("/api/chat/message")
    async def chat_message(
        req: ChatRequest,
        request: Request,
    ):
        """
        前端統一聊天入口。

        user_id：
            登入帳號。

        conversation_id：
            目前獨立對話。
        """

        user_id = deps.chat_user_id(request, req.uid)
        conversation_id = normalize_conversation_id(req.cid)

        security_started = time.perf_counter()
        guard_ticket = await deps.begin_chat_security(request, req, user_id)
        observe_pipeline_stage(
            "chat",
            "security_guard",
            security_started,
        )

        ticket = None
        try:
            admission_started = time.perf_counter()
            ticket = await deps.reserve_chat_ticket()
            observe_pipeline_stage(
                "chat",
                "admission_reserve",
                admission_started,
            )
            await deps.wait_for_chat_ticket(ticket)
            agent_session_started = time.perf_counter()
            agent = await run_in_threadpool(
                deps.get_agent_with_profile,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            observe_pipeline_stage(
                "chat",
                "agent_session",
                agent_session_started,
            )

            conversation_limit_started = time.perf_counter()
            history_for_limit = await deps.enforce_conversation_limit(
                agent=agent,
                request=request,
                user_id=user_id,
                incoming_message=req.message,
            )
            if not history_for_limit:
                await deps.guard_conversation_creation(request, user_id)
            observe_pipeline_stage(
                "chat",
                "conversation_limit",
                conversation_limit_started,
            )
            freshness_context = await _load_freshness_context(req.message)
            if req.flow == "skin_consult":
                result = await run_in_threadpool(
                    agent.process,
                    f"__knowledge_consult__:{req.message}",
                    conversation_flow=req.flow,
                    action=req.action,
                    freshness_context=freshness_context,
                )
            else:
                result = await run_in_threadpool(
                    agent.process,
                    req.message,
                    conversation_flow=req.flow,
                    action=req.action,
                    freshness_context=freshness_context,
                )
        finally:
            if ticket is not None:
                await ticket.release()
            await deps.finish_chat_security(guard_ticket)

        if isinstance(result, dict):
            result["conversation_limit"] = deps.completed_conversation_limit(
                history_for_limit, req.message, result
            )
        if isinstance(
            result.get("reply"),
            str,
        ):
            result["reply"] = (
                result["reply"]
                .replace(
                    "**",
                    "",
                )
            )

        result["route"] = (
            req.flow
            if req.flow in {
                "product_search",
                "skin_record",
                "skin_consult",
            }
            else result.get("route")
            or deps.stage_to_route(
                result.get("stage", "")
            )
        )

        result["sessionId"] = (
            conversation_id
        )

        result["conversationId"] = (
            conversation_id
        )

        return result


    @router.post("/api/chat/message/stream")
    async def chat_message_stream(
        req: ChatRequest,
        request: Request,
    ):
        """SSE 聊天入口；知識諮詢會直接轉送模型文字增量。"""
        stream_started_at = time.perf_counter()
        user_id = deps.chat_user_id(request, req.uid)
        conversation_id = normalize_conversation_id(req.cid)
        request_id = _request_id(request)
        last_event_id = _last_event_id(request)
        if request_id is not None and request.headers.get("Last-Event-ID", "").strip():
            existing_job = await chat_job_store.get(request_id, user_id=user_id)
            if existing_job is not None:
                return _streaming_response(
                    _replay_event_stream(
                        request_id,
                        user_id=user_id,
                        last_event_id=last_event_id,
                    )
                )

        guard_ticket = await deps.begin_chat_security(request, req, user_id)
        if request_id is not None:
            created = await chat_job_store.start(
                request_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if not created:
                await deps.finish_chat_security(guard_ticket)
                return _streaming_response(
                    _replay_event_stream(
                        request_id,
                        user_id=user_id,
                        last_event_id=last_event_id,
                    )
                )
        try:
            queue_started_at = time.perf_counter()
            ticket = await deps.reserve_chat_ticket()
            queue_wait_ms = (time.perf_counter() - queue_started_at) * 1000
            agent = await run_in_threadpool(
                deps.get_agent_with_profile,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            history_for_limit = await deps.enforce_conversation_limit(
                agent=agent,
                request=request,
                user_id=user_id,
                incoming_message=req.message,
            )
            if not history_for_limit:
                await deps.guard_conversation_creation(request, user_id)
        except BaseException:
            await deps.finish_chat_security(guard_ticket)
            raise

        async def event_stream():
            loop = asyncio.get_running_loop()
            producer = None
            events = asyncio.Queue()
            state = {"has_delta": False, "first_token_observed": False}


            async def queue_event(event: str, payload: dict[str, Any]) -> None:
                if event == "delta" and not state["first_token_observed"]:
                    state["first_token_observed"] = True
                    observe_pipeline_stage("chat", "first_token", stream_started_at)
                event_id = None
                if request_id is not None:
                    event_id = await chat_job_store.append_event(
                        request_id,
                        user_id=user_id,
                        event=event,
                        payload=payload,
                    )
                await events.put((event, payload, event_id))

            def on_delta(delta: str) -> None:
                if not delta:
                    return
                state["has_delta"] = True
                loop.call_soon_threadsafe(
                    asyncio.create_task,
                    queue_event("delta", {"text": delta}),
                )

            def run_agent(freshness_context=None) -> dict:
                process_input = (
                    f"__knowledge_consult__:{req.message}"
                    if req.flow == "skin_consult"
                    else req.message
                )
                return agent.process(
                    process_input,
                    conversation_flow=req.flow,
                    action=req.action,
                    on_delta=on_delta if req.flow == "skin_consult" else None,
                    freshness_context=freshness_context,
                )

            async def produce() -> None:
                try:
                    freshness_context = await _load_freshness_context(req.message)
                    result = await run_in_threadpool(run_agent, freshness_context)
                    if isinstance(result, dict):
                        result["conversation_limit"] = deps.completed_conversation_limit(
                            history_for_limit, req.message, result
                        )
                        if isinstance(result.get("reply"), str):
                            result["reply"] = result["reply"].replace("**", "")
                        result["route"] = (
                            req.flow
                            if req.flow in {
                                "product_search",
                                "skin_record",
                                "skin_consult",
                            }
                            else result.get("route")
                            or deps.stage_to_route(result.get("stage", ""))
                        )
                        result["sessionId"] = conversation_id
                        result["conversationId"] = conversation_id
                        reply = str(result.get("reply") or "")
                        if not state["has_delta"] and reply:
                            state["has_delta"] = True
                            await queue_event("delta", {"text": reply})
                        await queue_event("done", result)
                        if request_id is not None:
                            await chat_job_store.complete(
                                request_id,
                                user_id=user_id,
                                result=result,
                            )
                except Exception:
                    payload = {"message": "目前暫時無法完成回覆，請稍後再試。"}
                    await queue_event("error", payload)
                    if request_id is not None:
                        await chat_job_store.fail(
                            request_id,
                            user_id=user_id,
                            message=payload["message"],
                        )

            try:
                try:
                    if not ticket.acquired:
                        yield _encode_sse(
                            "status",
                            {"stage": "queued", "message": "目前使用人數較多，已進入排隊…"},
                        )
                    queue_seconds = await deps.wait_for_chat_ticket(ticket)
                except HTTPException as error:
                    yield _encode_sse(
                        "error",
                        {"message": str(error.detail), "retry_after": 5},
                    )
                    return

                yield _encode_sse(
                    "status",
                    {
                        "stage": "processing",
                        "message": "正在整理資料…",
                        "queue_ms": round(queue_seconds * 1000, 2),
                    },
                )
                producer = asyncio.create_task(produce())
                while True:
                    try:
                        event, payload, event_id = await asyncio.wait_for(events.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield _encode_sse(
                            "status",
                            {
                                "stage": "processing",
                                "message": "仍在處理中，完成後會自動顯示結果。",
                            },
                        )
                        continue
                    yield _encode_sse(event, payload, event_id=event_id)
                    if event in {"done", "error"}:
                        break
            finally:
                    if producer is not None and not producer.done():
                        # run_in_threadpool cannot reliably cancel an already-running
                        # sync SDK call.  Keep both admission and account locks until
                        # it really exits instead of allowing a cost-amplifying retry.
                        asyncio.create_task(
                            _release_stream_resources_when_done(
                            producer,
                            ticket,
                            guard_ticket,
                            deps.finish_chat_security,
                            release_started_at=time.perf_counter(),
                        )
                        )
                    else:
                        await ticket.release()
                        await deps.finish_chat_security(guard_ticket)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "Server-Timing": f"queue;dur={queue_wait_ms:.3f}",
            },
        )


    @router.post("/api/v1/session/clear")
    def clear_session(
        request: Request,
        user_id: str = "anonymous",
        conversation_id: str = "default",
    ):
        user_id = deps.chat_user_id(request, user_id)
        if (
            not user_id
            or user_id == "anonymous"
        ):
            raise HTTPException(
                status_code=400,
                detail="缺少登入 user_id",
            )

        normalized_conversation_id = (
            normalize_conversation_id(
                conversation_id
            )
        )

        agent = deps.get_agent(
            user_id=user_id,
            conversation_id=
                normalized_conversation_id,
        )

        try:
            agent.memory.clear_thread()

        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail=(
                    "清除 GetZep 對話失敗："
                    f"{error}"
                ),
            )

        agent.state.reset()

        return {
            "ok": True,
            "user_id":
                user_id,

            "conversation_id":
                normalized_conversation_id,

            "message":
                "已清除指定的 GetZep 對話",
        }


    return router
