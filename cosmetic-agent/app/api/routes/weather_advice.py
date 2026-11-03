"""Loopback-only weather advice endpoint for the frontend server."""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.services.weather_context_service import generate_weather_advice


class WeatherAdviceRequest(BaseModel):
    location: str = Field(min_length=1, max_length=80)
    temperature: float = Field(ge=-80, le=60)
    humidity: int = Field(ge=0, le=100)
    uv: float = Field(ge=0, le=30)
    rain: int = Field(ge=0, le=100)
    condition: str = Field(min_length=1, max_length=40)


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_weather_advice_router() -> APIRouter:
    router = APIRouter(tags=["internal"])

    @router.post("/internal/weather-advice")
    async def weather_advice(payload: WeatherAdviceRequest, request: Request):
        if not _is_loopback(request):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            advice = await run_in_threadpool(
                generate_weather_advice,
                **payload.model_dump(),
            )
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail="Weather advice unavailable",
            ) from error
        return {"advice": advice}

    return router
