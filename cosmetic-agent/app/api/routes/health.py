"""Health endpoints kept free of business and persistence logic."""

from collections.abc import Callable

from fastapi import APIRouter


def create_health_router(session_count: Callable[[], int]) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/api/v1/health")
    def health() -> dict[str, str]:
        # Public liveness checks intentionally expose no operational metrics.
        return {"status": "ok"}

    return router
