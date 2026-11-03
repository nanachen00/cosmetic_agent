"""Public endpoint for reviewed daily beauty content."""

from typing import Any

from fastapi import APIRouter, HTTPException

from database.postgres.repositories.daily_content import (
    postgres_daily_content_repository,
)


router = APIRouter(prefix="/api/daily-contents", tags=["daily-content"])


@router.get("")
def get_daily_contents() -> dict[str, Any]:
    try:
        result = postgres_daily_content_repository.get_daily_pair()
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="每日內容暫時無法取得",
        ) from error

    if len(result["contents"]) != 2:
        raise HTTPException(
            status_code=503,
            detail="每日內容尚未準備完成",
        )
    return result
