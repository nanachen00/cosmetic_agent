"""Verified-email registration routes.

RECONSTRUCTED 2026-10-21. Paths come from docs/architecture/API_ROUTE_INVENTORY.md:
  POST /api/auth/password/register/request
  POST /api/auth/password/register/confirm
The original request bodies are unknown (the login page source was not in the
export), so common field-name variants are accepted. Verify against the live
Vercel frontend before go-live.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import AliasChoices, BaseModel, Field

from database.repositories.email_registration import (
    EmailRegistrationError,
    EmailRegistrationRateLimitedError,
    InvalidEmailRegistrationCodeError,
)
from database.repositories.password_auth import (
    PasswordAuthValidationError,
    UsernameUnavailableError,
)

router = APIRouter(prefix="/api/auth/password/register", tags=["auth"])


class EmailRegistrationRequestBody(BaseModel):
    email: str = Field(..., max_length=254)


class EmailRegistrationConfirmBody(BaseModel):
    request_id: UUID = Field(validation_alias=AliasChoices("request_id", "challenge_id"))
    code: str = Field(..., max_length=16)
    username: str = Field(..., max_length=64)
    password: str = Field(
        ..., max_length=256, validation_alias=AliasChoices("password", "new_password")
    )


def register_email_registration_routes(
    *,
    service_factory: Callable[[], Any],
    issue_auth_session: Callable[..., Any],
) -> None:
    @router.post("/request", status_code=status.HTTP_202_ACCEPTED)
    def request_registration(req: EmailRegistrationRequestBody) -> dict[str, Any]:
        try:
            result = service_factory().request_code(email=req.email)
        except PasswordAuthValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except EmailRegistrationRateLimitedError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except EmailRegistrationError as error:
            raise HTTPException(status_code=502, detail="驗證信寄送失敗，請稍後再試") from error
        return {
            "ok": True,
            "request_id": str(result.request_id),
            "challenge_id": str(result.request_id),
            "expires_in_seconds": result.expires_in_seconds,
        }

    @router.post("/confirm")
    def confirm_registration(
        req: EmailRegistrationConfirmBody,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        try:
            account = service_factory().confirm_code(
                request_id=req.request_id,
                code=req.code,
                username=req.username,
                password=req.password,
            )
        except PasswordAuthValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except UsernameUnavailableError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InvalidEmailRegistrationCodeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except EmailRegistrationError as error:
            raise HTTPException(status_code=500, detail="建立會員帳號失敗") from error

        issue_auth_session(
            request=request,
            response=response,
            user_id=str(account.user_id),
            auth_method="password",
        )
        return {
            "ok": True,
            "user_id": str(account.user_id),
            "username": account.username,
            "email": account.email,
        }
