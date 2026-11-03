from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from app.services.daily_checkin_service import DailyCheckinService


def create_daily_checkin_router(*, service: DailyCheckinService, resolve_user_id: Callable[[str, Optional[str], Request], str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/checkin/status")
    def get_daily_checkin_status(request: Request, user_id: str = "anonymous", session_id: Optional[str] = None):
        """
        前端一進入 /daily-checkin 時呼叫。

        回傳：
        - 點數
        - 現在第幾輪
        - 本輪完成幾天
        - 今日是否已簽
        - Day 5 / Day 7 是否解鎖
        """
        uid = resolve_user_id(user_id, session_id, request)
        try:
            return service.build_status(uid)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to get check-in status")
            raise HTTPException(status_code=500, detail="取得簽到狀態失敗") from None

    @router.post("/api/checkin")
    def daily_checkin(request: Request, user_id: str = "anonymous", session_id: Optional[str] = None):
        """
        每日簽到。

        後端負責：
        - 判斷台灣今天日期
        - 防止同日重複簽到
        - 計算 Day 1 ~ Day 14
        - +10P
        - 記錄真正簽到時間
        - 判斷功能解鎖
        """
        uid = resolve_user_id(user_id, session_id, request)
        try:
            return service.check_in(uid)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to create daily check-in")
            raise HTTPException(status_code=500, detail="簽到失敗") from None

    @router.get("/api/checkin/history")
    def get_daily_checkin_history(request: Request, user_id: str = "anonymous", session_id: Optional[str] = None):
        """
        查詢使用者所有簽到時間。

        目前頁面不一定會顯示，
        但後端已完整保留紀錄，
        之後做歷史頁可以直接使用。
        """
        uid = resolve_user_id(user_id, session_id, request)
        try:
            return service.history(uid)
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Failed to get check-in history")
            raise HTTPException(status_code=500, detail="取得簽到歷史失敗") from None

    return router
