"""Welcome endpoint kept free of business and persistence logic."""

from collections.abc import Callable

from fastapi import APIRouter, Request

from app.services.welcome_service import WelcomeService

OwnerResolver = Callable[[Request, str], str]


def create_welcome_router(
    *,
    owner_resolver: OwnerResolver,
    welcome_service: WelcomeService,
) -> APIRouter:
    router = APIRouter(tags=["welcome"])

    @router.get("/api/v1/welcome")
    def welcome(request: Request, user_id: str = "anonymous"):
        owner_id = owner_resolver(request, user_id)
        return welcome_service.get_welcome(owner_id)

    return router
