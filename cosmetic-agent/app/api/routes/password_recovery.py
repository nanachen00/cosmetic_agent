"""Public password-recovery routes, isolated from existing OAuth routes."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel


router = APIRouter(prefix="/api/auth/password/recovery", tags=["auth"])


class PasswordRecoveryRequestBody(BaseModel):
    email: str


class PasswordRecoveryConfirmBody(BaseModel):
    request_id: UUID
    code: str
    new_password: str


def register_password_recovery_routes(
    *,
    recovery_service_factory: Callable[[], Any],
    issue_auth_session: Callable[..., Any],
    invalid_code_error: type[Exception],
    rate_limited_error: type[Exception],
    password_validation_error: type[Exception],
    recovery_error: type[Exception],
) -> None:
    @router.post("/request", status_code=status.HTTP_202_ACCEPTED)
    def request_recovery(req: PasswordRecoveryRequestBody) -> dict[str, Any]:
        try:
            result = recovery_service_factory().request_code(email=req.email)
        except rate_limited_error as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except recovery_error as error:
            raise HTTPException(
                status_code=502,
                detail="驗證信寄送失敗，請稍後再試",
            ) from error
        return {
            "ok": True,
            "request_id": str(result.request_id),
            "expires_in_seconds": result.expires_in_seconds,
        }

    @router.post("/confirm")
    def confirm_recovery(
        req: PasswordRecoveryConfirmBody,
        request: Request,
        response: Response,
    ) -> dict[str, bool | str]:
        try:
            user_id = recovery_service_factory().confirm_code(
                request_id=req.request_id,
                code=req.code,
                new_password=req.new_password,
            )
        except password_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except invalid_code_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except recovery_error as error:
            raise HTTPException(status_code=500, detail="重設密碼失敗") from error

        issue_auth_session(
            request=request,
            response=response,
            user_id=str(user_id),
            auth_method="email_reset",
        )
        return {"ok": True, "user_id": str(user_id)}
