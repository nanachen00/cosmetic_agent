"""可由主 application 掛載的 Skin Tracking APIRouter；不修改 Root 或全域 app 設定。"""
from __future__ import annotations

from datetime import date as Date
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.skin_tracking.extraction import SkinExtractionError
from agents.skin_tracking.observation_taxonomy import (
    validate_detail_zones,
    validate_tag_slugs,
)
from agents.skin_tracking.service import SkinRecordService, SkinTrackingService
from schemas.tracking_schema import Issue, Zone


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=128)

    @field_validator("message", "session_id", mode="before")
    @classmethod
    def _strip_required_text(cls, value):
        """Whitespace is not a valid message or browser session identifier."""
        return value.strip() if isinstance(value, str) else value


class UpdateRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: Optional[Issue] = None
    severity: Optional[int] = Field(None, ge=1, le=5)
    zone: Optional[Zone] = None
    tag_slugs: Optional[list[str]] = None
    detail_zones: Optional[list[str]] = None
    onset_date: Optional[Date] = None
    duration_days: Optional[int] = Field(None, ge=0)
    note: Optional[str] = None
    date: Optional[Date] = None

    @field_validator("tag_slugs", mode="before")
    @classmethod
    def _validate_tag_slugs(cls, value):
        return None if value is None else validate_tag_slugs(value)

    @field_validator("detail_zones", mode="before")
    @classmethod
    def _validate_detail_zones(cls, value):
        return None if value is None else validate_detail_zones(value)


def create_router(
    get_user_id: Callable,
    get_tracking_service: Callable[[], SkinTrackingService],
    get_record_service: Callable[[], SkinRecordService],
) -> APIRouter:
    router = APIRouter(prefix="/api/skin-tracking", tags=["skin-tracking"])

    @router.post("/message")
    def send_message(payload: ChatRequest, user_id: str = Depends(get_user_id)):
        try:
            return get_tracking_service().send(user_id, payload.session_id, payload.message)
        except SkinExtractionError as exc:
            # Provider errors may contain request metadata; the browser receives a stable state.
            raise HTTPException(
                status_code=503,
                detail="Skin Tracking is temporarily unavailable",
            ) from exc

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str, user_id: str = Depends(get_user_id)):
        result = get_tracking_service().get_session(user_id, session_id)
        if result is None:
            raise HTTPException(status_code=410, detail="Session expired")
        return result

    @router.get("/records")
    def list_records(
        date_from: Optional[Date] = None,
        date_to: Optional[Date] = None,
        issue: Optional[Issue] = None,
        zone: Optional[Zone] = None,
        user_id: str = Depends(get_user_id),
    ):
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
        records = get_record_service().list_records(
            user_id,
            date_from.isoformat() if date_from else None,
            date_to.isoformat() if date_to else None,
            issue.value if issue else None,
            zone.value if zone else None,
        )
        return {"records": records}

    @router.patch("/records/{record_id}")
    def update_record(
        record_id: str,
        payload: UpdateRecordRequest,
        user_id: str = Depends(get_user_id),
    ):
        fields = payload.model_dump(mode="json", exclude_none=True)
        if not fields:
            raise HTTPException(status_code=422, detail="No fields to update")
        if not get_record_service().update_record(user_id, record_id, fields):
            raise HTTPException(status_code=404, detail="Skin record not found")
        return {"updated": True, "record_id": record_id}

    @router.delete("/records/{record_id}")
    def delete_record(record_id: str, user_id: str = Depends(get_user_id)):
        if not get_record_service().delete_record(user_id, record_id):
            raise HTTPException(status_code=404, detail="Skin record not found")
        return {"deleted": True, "record_id": record_id}

    @router.get("/reports/current")
    def current_report(
        period_days: int = Query(default=14, ge=5, le=90),
        user_id: str = Depends(get_user_id),
    ):
        return get_record_service().current_report(user_id, period_days)

    @router.get("/reports/recommendations")
    def current_recommendations(
        period_days: int = Query(default=14, ge=5, le=90),
        limit: int = Query(default=3, ge=1, le=4),
        date_from: Optional[Date] = None,
        date_to: Optional[Date] = None,
        user_id: str = Depends(get_user_id),
    ):
        if (date_from is None) != (date_to is None):
            raise HTTPException(status_code=422, detail="date_from and date_to must be provided together")
        if date_from is not None and date_to is not None:
            if date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
            if (date_to - date_from).days >= 90:
                raise HTTPException(status_code=422, detail="date range must not exceed 90 days")
        return get_record_service().current_recommendations(
            user_id,
            period_days=period_days,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )

    @router.get("/reports/partner-recommendations")
    def partner_recommendations(
        period_days: int = Query(default=14, ge=5, le=90),
        limit: int = Query(default=3, ge=1, le=4),
        date_from: Optional[Date] = None,
        date_to: Optional[Date] = None,
        user_id: str = Depends(get_user_id),
    ):
        if (date_from is None) != (date_to is None):
            raise HTTPException(status_code=422, detail="date_from and date_to must be provided together")
        if date_from is not None and date_to is not None:
            if date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
            if (date_to - date_from).days >= 90:
                raise HTTPException(status_code=422, detail="date range must not exceed 90 days")
        return get_record_service().partner_recommendations(
            user_id,
            period_days=period_days,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )

    return router
