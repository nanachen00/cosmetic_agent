"""HTTP boundary for skin-record CRUD and current reports."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.skin_record_service import SkinRecordService


class SkinRecordCreate(BaseModel):
    user_id: str = "anonymous"
    issue: str
    zone: str
    severity: int
    note: str = ""
    products: list[str] = []
    duration_days: Optional[int] = None


class SkinRecordPatch(BaseModel):
    user_id: str = "anonymous"
    severity: Optional[int] = None
    note: Optional[str] = None
    issue: Optional[str] = None
    zone: Optional[str] = None


class FrontendSkinRecord(BaseModel):
    recordDate: Optional[str] = None
    region: str
    conditions: list[str]
    severity: int
    notes: str = ""
    user_id: Optional[str] = None
    session_id: Optional[str] = None


def create_skin_record_router(
    *,
    owner_resolver: Callable[[Request, str], str],
    service: SkinRecordService,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/skin-records")
    def create_skin_record(req: SkinRecordCreate, request: Request):
        """膚況紀錄表單直存（結構化路徑，不經 TAIDE）
        走 save_skin_record 以套用同學的標準化＋連勝天數計算"""
        if not 1 <= req.severity <= 5:
            raise HTTPException(400, "severity 必須是 1~5")
        try:
            result = service.create(
                user_id=owner_resolver(request, req.user_id),
                issue=req.issue,
                zone=req.zone,
                severity=req.severity,
                note=req.note,
                products=req.products,
                duration_days=req.duration_days,
            )
            return {"ok": True, **result}
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to save skin record")
            raise HTTPException(status_code=500, detail="存檔失敗") from None

    @router.get("/api/skin-records")
    def list_skin_records(
        request: Request,
        user_id: str = "anonymous",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ):
        """歷史紀錄查詢（date_from/date_to 格式 YYYY-MM-DD，可省略）"""
        try:
            records = service.list(
                user_id=owner_resolver(request, user_id),
                date_from=date_from,
                date_to=date_to,
            )
            return {"ok": True, "count": len(records), "records": records}
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to list skin records")
            raise HTTPException(status_code=500, detail="查詢失敗") from None

    @router.patch("/api/skin-records/{record_id}")
    def patch_skin_record(record_id: str, req: SkinRecordPatch, request: Request):
        """修改紀錄（只更新有帶的欄位）"""
        fields = {key: value for key, value in req.model_dump().items() if key != "user_id" and value is not None}
        if not fields:
            raise HTTPException(400, "沒有要更新的欄位")
        if "severity" in fields and not 1 <= fields["severity"] <= 5:
            raise HTTPException(400, "severity 必須是 1~5")
        updated = service.patch(
            user_id=owner_resolver(request, req.user_id),
            record_id=record_id,
            fields=fields,
        )
        if not updated:
            raise HTTPException(404, "找不到這筆紀錄")
        return {"ok": True, "updated": list(fields.keys())}

    @router.delete("/api/skin-records/{record_id}")
    def delete_skin_record(record_id: str, request: Request, user_id: str = "anonymous"):
        deleted = service.delete(
            user_id=owner_resolver(request, user_id),
            record_id=record_id,
        )
        if not deleted:
            raise HTTPException(404, "找不到這筆紀錄，可能已被刪除")
        return {"ok": True}

    @router.get("/api/skin-reports/current")
    def current_skin_report(request: Request, user_id: str = "anonymous"):
        """最新膚況分析；保留進度欄位，但內容不再受天數限制。"""
        user_id = owner_resolver(request, user_id)
        try:
            eligibility = service.eligibility(user_id=user_id)
        except Exception as error:
            raise HTTPException(500, f"資格查詢失敗：{error}") from error
        eligibility = {**eligibility, "eligible": True, "days_needed": 0}
        text, report = service.build_current_report(user_id=user_id)
        return {
            "ok": True,
            "eligible": True,
            "eligibility": eligibility,
            "report_text": text,
            "report": report.model_dump() if hasattr(report, "model_dump") else report,
        }

    @router.post("/api/skin-records/frontend")
    def create_skin_record_frontend(req: FrontendSkinRecord, request: Request):
        """前端 SkinRecordPayload 直入口（欄位映射後走同一 save）"""
        user_id = owner_resolver(request, req.user_id or req.session_id or "anonymous")
        try:
            result = service.create_from_frontend(
                user_id=user_id,
                region=req.region,
                conditions=req.conditions,
                severity=req.severity,
                notes=req.notes,
                record_date=req.recordDate,
            )
            return {"ok": True, **result}
        except Exception as error:
            raise HTTPException(500, f"存檔失敗：{error}") from error

    return router
