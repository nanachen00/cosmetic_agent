"""Quiz HTTP routes with injected business services."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


class QuizSubmitRequest(BaseModel):
    user_id: str
    selected_answers: dict[str, str]
    session_id: Optional[str] = None
    message_id: Optional[str] = None


def create_quiz_router(
    *,
    require_auth_session: Callable[[Request], dict[str, Any]],
    get_questions: Callable[[], list[dict[str, Any]]],
    record_result: Callable[..., dict[str, Any]],
    get_latest_result: Callable[..., dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/quiz", tags=["quiz"])

    def canonical_user_id(request: Request, proposed: str) -> str:
        try:
            return str(require_auth_session(request)["user_id"])
        except HTTPException:
            candidate = str(proposed or "").strip().lower()
            if candidate.startswith("guest-"):
                try:
                    parsed = UUID(candidate[6:])
                except (ValueError, AttributeError):
                    parsed = None
                if parsed is not None and parsed.version == 4 and candidate == f"guest-{parsed}":
                    return candidate
            raise HTTPException(status_code=401, detail="Authentication required")

    def profile_repository():
        if os.getenv("AUTH_STORAGE_BACKEND", "postgres").strip().lower() == "postgres":
            from database.postgres.repositories.profile import (
                postgres_profile_repository,
            )

            return postgres_profile_repository
        from database.profile_repository import profile_repository

        return profile_repository

    @router.get("/questions")
    def get_customer_quiz_questions() -> dict[str, Any]:
        return {"ok": True, "questions": get_questions()}

    @router.post("/submit")
    def submit_customer_quiz(
        payload: QuizSubmitRequest,
        request: Request,
    ) -> dict[str, Any]:
        user_id = canonical_user_id(request, payload.user_id)
        result = record_result(
            user_id=user_id,
            selected_answers=payload.selected_answers,
            session_id=payload.session_id,
            message_id=payload.message_id,
        )
        primary_type = str(result.get("primary_type", "") or "").strip()
        if primary_type and not user_id.startswith("guest-"):
            try:
                profile_repository().update_customer_type(
                    user_id=user_id,
                    customer_type=primary_type,
                )
            except Exception as error:
                # Quiz remains successful even if profile mirroring is temporarily down.
                print(f"[quiz] 同步消費人格到 Profile 失敗：{error}")
        return result

    @router.get("/latest")
    def get_customer_latest_quiz(
        request: Request,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            return get_latest_result(
                user_id=canonical_user_id(request, user_id),
            )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=f"取得 Quiz 結果失敗：{error}",
            ) from error

    return router
