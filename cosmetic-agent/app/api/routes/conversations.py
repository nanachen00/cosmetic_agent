"""HTTP routes for conversation lifecycle and history."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.services.conversation_service import (
    ConversationQuotaExceeded,
    ConversationService,
)


class ConversationCreateRequest(BaseModel):
    user_id: str


class ConversationRenameRequest(BaseModel):
    user_id: str = "anonymous"
    title: str


def create_conversation_router(
    *,
    owner_resolver: Callable[[Request, str], str],
    service: ConversationService,
) -> APIRouter:
    router = APIRouter()

    def require_owner(request: Request, proposed_user_id: str) -> str:
        user_id = owner_resolver(request, proposed_user_id)
        if not user_id or user_id == "anonymous":
            raise HTTPException(status_code=400, detail="缺少登入 user_id")
        return user_id

    @router.get("/api/conversations")
    def list_conversations(
        request: Request,
        user_id: str = "anonymous",
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ):
        user_id = require_owner(request, user_id)
        try:
            conversations, total = service.list_page(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
            return {
                "ok": True,
                "user_id": user_id,
                "conversations": conversations,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(conversations) < total,
                "max_conversations": service.max_conversations_per_user,
            }
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail=f"讀取對話清單失敗：{error}",
            ) from error

    @router.post("/api/conversations")
    async def create_conversation(payload: ConversationCreateRequest, request: Request):
        user_id = require_owner(request, payload.user_id)
        try:
            conversation = await run_in_threadpool(
            service.create_draft,
            user_id=user_id,
        )
        except ConversationQuotaExceeded as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conversation_quota_exceeded",
                    "current": error.current,
                    "maximum": error.maximum,
                },
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail="建立對話失敗") from error
        return {
            "ok": True,
            "conversation": conversation,
            "quota_enforcement": service.quota_enforcement_mode,
        }

    @router.patch("/api/conversations/{conversation_id}")
    def rename_conversation(
        conversation_id: str,
        payload: ConversationRenameRequest,
        request: Request,
    ):
        user_id = require_owner(request, payload.user_id)
        try:
            conversation = service.rename(
                user_id=user_id,
                conversation_id=conversation_id,
                title=payload.title,
            )
            return {"ok": True, "conversation": conversation}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail="找不到指定的對話") from error
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail="重新命名失敗") from error

    @router.delete("/api/conversations/{conversation_id}")
    def delete_conversation(
        conversation_id: str,
        request: Request,
        user_id: str = "anonymous",
    ):
        """刪除登入使用者的指定對話。"""
        normalized_user_id = require_owner(request, user_id)
        try:
            normalized_conversation_id = service.delete(
                user_id=normalized_user_id,
                conversation_id=conversation_id,
            )
            return {
                "ok": True,
                "user_id": normalized_user_id,
                "conversation_id": normalized_conversation_id,
                "message": "對話已刪除",
            }
        except HTTPException:
            raise
        except Exception:
            # Keep provider names, identifiers, and exception messages out of
            # the response.  A failed delete is retryable and remains visible.
            raise HTTPException(
                status_code=503,
                detail="對話刪除暫時無法完成，請稍後再試。",
            ) from None

    @router.get("/api/chat/history")
    def chat_history(
        request: Request,
        user_id: str = "anonymous",
        conversation_id: str = "default",
        lastn: int = 100,
    ):
        user_id = require_owner(request, user_id)
        try:
            messages, limit = service.history(
                user_id=user_id,
                conversation_id=conversation_id,
                lastn=lastn,
            )
            return {
                "ok": True,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "messages": messages,
                "conversation_limit": limit,
            }
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail=f"讀取 GetZep 對話失敗：{error}",
            ) from error

    return router
