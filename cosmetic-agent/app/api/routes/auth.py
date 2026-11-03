"""Authentication HTTP routes.

Routes are moved here incrementally from the legacy API module. Public paths
and response contracts must remain unchanged during the migration.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas.auth import (
    ChangePasswordRequest,
    ChangeUsernameRequest,
    ConfirmPasswordResetRequest,
    PasswordLoginRequest,
    PasswordRegisterRequest,
    SetPasswordRequest,
    UsernameClaimRequest,
    VerifyPasswordRequest,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/identity")
def create_login_identity() -> None:
    """Keep the retired email-only identity endpoint explicitly unavailable."""
    raise HTTPException(
        status_code=410,
        detail="舊版 Email 假登入已停用，請使用會員帳號登入",
    )


def register_session_routes(
    *,
    require_auth_session: Callable[..., dict[str, Any]],
    password_auth_service_factory: Callable[[], Any],
    auth_session_service_factory: Callable[[], Any],
    session_cookie_name: str,
) -> None:
    """Register session routes while legacy services are injected by the caller."""

    @router.get("/session")
    def get_auth_session(request: Request) -> dict[str, Any]:
        session_account = require_auth_session(request)
        account = password_auth_service_factory().get_user_account(
            session_account["user_id"]
        )
        if account is None:
            raise HTTPException(status_code=404, detail="找不到會員資料")
        return {
            "ok": True,
            "auth_method": session_account["auth_method"],
            **account,
        }

    @router.post("/logout")
    def logout_auth_session(request: Request, response: Response) -> dict[str, bool]:
        token = request.cookies.get(session_cookie_name, "")
        auth_session_service_factory().revoke(token)
        response.delete_cookie(key=session_cookie_name, path="/")
        return {"ok": True}


def register_provider_management_routes(
    *,
    require_auth_session: Callable[..., dict[str, Any]],
    password_auth_service_factory: Callable[[], Any],
    last_login_method_error: type[Exception],
    account_not_found_error: type[Exception],
    password_auth_error: type[Exception],
) -> None:
    """Register authenticated OAuth provider unlinking."""

    @router.delete("/providers/{provider}")
    def unlink_oauth_provider(
        provider: str,
        request: Request,
    ) -> dict[str, Any]:
        normalized_provider = (provider or "").strip().lower()
        if normalized_provider not in {"google", "line"}:
            raise HTTPException(status_code=400, detail="不支援的快速登入來源")

        session_account = require_auth_session(request)
        try:
            account = password_auth_service_factory().unlink_oauth_identity(
                user_id=session_account["user_id"],
                provider=normalized_provider,
            )
        except last_login_method_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except account_not_found_error as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(
                status_code=500,
                detail="解除帳號綁定服務暫時無法使用",
            ) from error
        return {
            "ok": True,
            "auth_method": session_account["auth_method"],
            **account,
        }


def register_username_routes(
    *,
    require_auth_session: Callable[..., dict[str, Any]],
    password_auth_service_factory: Callable[[], Any],
    username_validation_error: type[Exception],
    username_already_exists_error: type[Exception],
    username_change_cooldown_error: type[Exception],
    account_not_found_error: type[Exception],
    password_auth_error: type[Exception],
) -> None:
    """Register username checks without coupling this module to storage code."""

    @router.get("/username/availability")
    def check_username_availability(username: str) -> dict[str, Any]:
        try:
            available = password_auth_service_factory().is_username_available(username)
        except username_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "available": available}

    @router.patch("/username")
    def change_username(
        req: ChangeUsernameRequest,
        request: Request,
    ) -> dict[str, Any]:
        session_account = require_auth_session(request)
        try:
            account = password_auth_service_factory().change_username(
                user_id=session_account["user_id"],
                username=req.username,
            )
        except username_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (
            username_already_exists_error,
            username_change_cooldown_error,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except account_not_found_error as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(
                status_code=500,
                detail="會員帳號修改服務暫時無法使用",
            ) from error
        return {
            "ok": True,
            "auth_method": session_account["auth_method"],
            **account,
        }


def register_onboarding_status_route(
    *,
    password_auth_service_factory: Callable[[], Any],
    verify_onboarding_token: Callable[[str], dict[str, Any]],
    onboarding_token_error: type[Exception],
    onboarding_cookie_name: str,
    username_validation_error: type[Exception],
    username_already_exists_error: type[Exception],
    username_already_set_error: type[Exception],
    account_not_found_error: type[Exception],
    password_auth_error: type[Exception],
) -> None:
    """Register the read-only onboarding status endpoint."""

    def read_onboarding_payload(request: Request) -> dict[str, Any]:
        token = request.cookies.get(onboarding_cookie_name, "")
        try:
            return verify_onboarding_token(token)
        except onboarding_token_error as error:
            raise HTTPException(status_code=401, detail=str(error)) from error

    @router.get("/onboarding/status")
    def get_oauth_onboarding_status(
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        payload = read_onboarding_payload(request)
        account = password_auth_service_factory().get_user_account(payload["user_id"])
        if account is None:
            raise HTTPException(
                status_code=404,
                detail="找不到快速登入會員資料，請重新登入",
            )
        if account["username"]:
            response.delete_cookie(key=onboarding_cookie_name, path="/")
        return {"ok": True, "provider": payload["provider"], **account}

    @router.post("/onboarding/username")
    def claim_oauth_username(
        req: UsernameClaimRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        payload = read_onboarding_payload(request)
        try:
            account = password_auth_service_factory().claim_username(
                user_id=payload["user_id"],
                username=req.username,
            )
        except username_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except username_already_exists_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except username_already_set_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except account_not_found_error as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(
                status_code=500,
                detail="會員帳號設定服務暫時無法使用",
            ) from error

        response.delete_cookie(key=onboarding_cookie_name, path="/")
        return {"ok": True, **account}


def register_password_entry_routes(
    *,
    password_auth_service_factory: Callable[[], Any],
    issue_auth_session: Callable[..., Any],
    client_host: Callable[[Request], str],
    username_validation_error: type[Exception],
    password_validation_error: type[Exception],
    username_already_exists_error: type[Exception],
    login_rate_limited_error: type[Exception],
    invalid_credentials_error: type[Exception],
    password_auth_error: type[Exception],
) -> None:
    """Register account creation and password-login entry points."""

    @router.post("/password/register")
    def register_password_account(
        req: PasswordRegisterRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        try:
            account = password_auth_service_factory().register(
                username=req.username,
                password=req.password,
            )
        except username_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except password_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except username_already_exists_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(status_code=500, detail="建立會員帳號失敗") from error

        issue_auth_session(
            request=request,
            response=response,
            user_id=account["user_id"],
            auth_method="password",
        )
        return {"ok": True, **account}

    @router.post("/password/login")
    def login_password_account(
        req: PasswordLoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        try:
            account = password_auth_service_factory().login(
                username=req.username,
                password=req.password,
                client_key=client_host(request),
            )
        except login_rate_limited_error as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except invalid_credentials_error as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(
                status_code=500,
                detail="會員登入服務暫時無法使用",
            ) from error

        issue_auth_session(
            request=request,
            response=response,
            user_id=account["user_id"],
            auth_method="password",
        )
        return {"ok": True, **account}


def register_password_management_routes(
    *,
    require_auth_session: Callable[..., dict[str, Any]],
    password_auth_service_factory: Callable[[], Any],
    password_reset_service_factory: Callable[[], Any],
    auth_session_service_factory: Callable[[], Any],
    issue_auth_session: Callable[..., Any],
    client_host: Callable[[Request], str],
    password_validation_error: type[Exception],
    password_already_set_error: type[Exception],
    account_not_found_error: type[Exception],
    invalid_credentials_error: type[Exception],
    login_rate_limited_error: type[Exception],
    password_not_set_error: type[Exception],
    password_auth_error: type[Exception],
    password_reset_not_configured_error: type[Exception],
    verified_email_required_error: type[Exception],
    password_reset_rate_limited_error: type[Exception],
    invalid_password_reset_code_error: type[Exception],
    password_reset_error: type[Exception],
) -> None:
    """Register password setup, change, verification, and reset routes."""

    @router.post("/password/set")
    def set_initial_password(
        req: SetPasswordRequest,
        request: Request,
    ) -> dict[str, Any]:
        session_account = require_auth_session(request, require_recent=True)
        try:
            account = password_auth_service_factory().set_initial_password(
                user_id=session_account["user_id"],
                password=req.new_password,
            )
        except password_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except password_already_set_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except account_not_found_error as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(status_code=500, detail="設定會員密碼失敗") from error
        return {"ok": True, **account, "password_set": True}

    @router.post("/password/change")
    def change_password(
        req: ChangePasswordRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        session_account = require_auth_session(request)
        try:
            account = password_auth_service_factory().change_password(
                user_id=session_account["user_id"],
                current_password=req.current_password,
                new_password=req.new_password,
                client_key=client_host(request),
            )
        except password_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except invalid_credentials_error as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except login_rate_limited_error as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except password_not_set_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except password_auth_error as error:
            raise HTTPException(status_code=500, detail="變更會員密碼失敗") from error

        auth_session_service_factory().revoke_all_for_user(account["user_id"])
        issue_auth_session(
            request=request,
            response=response,
            user_id=account["user_id"],
            auth_method="password",
        )
        return {"ok": True, **account}

    @router.post("/password/verify")
    def verify_current_password(
        req: VerifyPasswordRequest,
        request: Request,
    ) -> dict[str, bool]:
        session_account = require_auth_session(request)
        try:
            password_auth_service_factory().verify_current_password(
                user_id=session_account["user_id"],
                current_password=req.current_password,
                client_key=client_host(request),
            )
        except invalid_credentials_error as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except login_rate_limited_error as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except password_not_set_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"ok": True}

    @router.post("/password/reset/request")
    def request_password_reset(request: Request) -> dict[str, Any]:
        session_account = require_auth_session(request)
        try:
            result = password_reset_service_factory().request_code(
                user_id=session_account["user_id"]
            )
        except password_reset_not_configured_error as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except verified_email_required_error as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except password_reset_rate_limited_error as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except password_reset_error as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {"ok": True, **result}

    @router.post("/password/reset/confirm")
    def confirm_password_reset(
        req: ConfirmPasswordResetRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        session_account = require_auth_session(request)
        try:
            password_reset_service_factory().consume_code(
                user_id=session_account["user_id"],
                challenge_id=req.challenge_id,
                code=req.code,
            )
            account = password_auth_service_factory().reset_password(
                user_id=session_account["user_id"],
                new_password=req.new_password,
            )
        except password_validation_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except invalid_password_reset_code_error as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except password_reset_not_configured_error as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except password_reset_error as error:
            raise HTTPException(status_code=500, detail="重設密碼失敗") from error
        except password_auth_error as error:
            raise HTTPException(status_code=500, detail="重設密碼失敗") from error

        auth_session_service_factory().revoke_all_for_user(account["user_id"])
        issue_auth_session(
            request=request,
            response=response,
            user_id=account["user_id"],
            auth_method="email_reset",
        )
        return {"ok": True, **account}
