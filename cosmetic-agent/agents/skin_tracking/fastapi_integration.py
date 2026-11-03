"""把 Skin Tracking 掛入既有 FastAPI app，不接管 Root 或其他 Agent。"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agents.skin_tracking.bootstrap import (
    create_skin_tracking_router,
    initialize_skin_tracking,
    shutdown_skin_tracking,
)
from agents.skin_tracking.composition import SkinTrackingConfigurationError

INSTALLATION_STATE_KEY = "skin_tracking_installed"


def resolve_skin_tracking_user_id(request: Request) -> str:
    """暫用請求標頭識別使用者；之後可由正式登入依賴取代。"""
    user_id = request.headers.get("X-User-ID", "").strip()
    if not user_id:
        return "anonymous"
    if len(user_id) > 128:
        raise HTTPException(status_code=422, detail="X-User-ID is too long")
    return user_id


def install_skin_tracking(
    app: FastAPI,
    *,
    get_user_id: Callable = resolve_skin_tracking_user_id,
) -> None:
    """安裝一次路由與生命週期；初始化失敗時不阻斷其他 Agent。"""
    if getattr(app.state, INSTALLATION_STATE_KEY, False):
        return

    app.include_router(create_skin_tracking_router(get_user_id))
    app.add_exception_handler(
        SkinTrackingConfigurationError,
        _skin_tracking_unavailable,
    )
    # AI Stack 目前的 FastAPI 版本沒有 app.add_event_handler()；
    # router event list 在新舊版本都可用，也不會改寫既有 lifespan。
    app.router.on_startup.append(_start_skin_tracking)
    app.router.on_shutdown.append(shutdown_skin_tracking)
    setattr(app.state, INSTALLATION_STATE_KEY, True)


def _start_skin_tracking() -> None:
    """延後到 app startup 建立連線，避免 import 階段影響整個服務。"""
    try:
        initialize_skin_tracking()
    except SkinTrackingConfigurationError:
        # 路由會回傳固定 503；例外內容可能含部署資訊，因此不向外輸出。
        return


async def _skin_tracking_unavailable(
    _request: Request,
    _exc: SkinTrackingConfigurationError,
) -> JSONResponse:
    """依賴未就緒只影響 Skin Tracking，不讓全域 API 一起停止。"""
    return JSONResponse(
        status_code=503,
        content={"detail": "Skin Tracking is temporarily unavailable"},
    )
