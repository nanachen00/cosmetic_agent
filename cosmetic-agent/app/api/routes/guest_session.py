"""HTTP boundary for issuing and refreshing signed guest sessions."""

from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, Request, Response


class GuestSessionSigner(Protocol):
    def verify(self, token: str) -> str | None: ...

    def issue(self) -> tuple[str, str]: ...


SignerProvider = Callable[[], GuestSessionSigner]


def create_guest_session_router(
    *,
    signer_provider: SignerProvider,
    cookie_name: str,
    cookie_ttl_seconds: int,
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.post("/api/auth/guest-session")
    def issue_guest_session(request: Request, response: Response):
        """Issue a server-bound guest identity; never trust a browser-generated ID."""
        signer = signer_provider()
        cookie = request.cookies.get(cookie_name, "")
        guest_id = signer.verify(cookie)
        if not guest_id:
            guest_id, cookie = signer.issue()
            hostname = (request.url.hostname or "").lower()
            response.set_cookie(
                key=cookie_name,
                value=cookie,
                max_age=cookie_ttl_seconds,
                httponly=True,
                secure=hostname not in {"localhost", "127.0.0.1"},
                samesite="lax",
                path="/",
            )
        response.headers["Cache-Control"] = "no-store"
        return {"user_id": guest_id}

    return router
