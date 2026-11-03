from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from tools.auth.google_login import (
    build_google_authorization_url,
    create_blink_user_id_from_google,
    create_google_state,
    exchange_google_code,
    get_google_profile,
)
from tools.auth.line_login import (
    build_line_authorization_url,
    create_blink_user_id_from_line,
    create_line_state,
    exchange_line_code,
    get_line_profile,
    verify_line_id_token,
)
from tools.auth.oauth_onboarding import (
    ONBOARDING_COOKIE_NAME,
    ONBOARDING_TOKEN_TTL_SECONDS,
    issue_onboarding_token,
    onboarding_cookie_samesite,
    onboarding_cookie_secure,
)
from tools.auth.password_login import (
    EmailIdentityConflictError,
    OAuthIdentityAlreadyLinkedError,
    create_password_auth_service,
)

OAUTH_LINK_COOKIE_NAME = (
    os.getenv("OAUTH_LINK_COOKIE_NAME", "blink_oauth_link_provider").strip()
    or "blink_oauth_link_provider"
)


@dataclass(frozen=True)
class OAuthRouteDependencies:
    require_auth_session: Any
    oauth_callback_url: Any
    oauth_success_url: Any
    frontend_profile_url: Any
    issue_auth_session: Any


def create_oauth_router(deps: OAuthRouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/line/login")
    def line_login(
        request: Request,
    ):
        """
        開始 LINE Login。

        要求權限：

        - profile
        - openid
        - email

        email permission 必須已經在
        LINE Developers Console 顯示 Applied。
        """

        try:
            link_mode = (
                request.query_params.get(
                    "mode"
                )
                == "link"
            )

            if link_mode:
                deps.require_auth_session(
                    request
                )

            state = (
                create_line_state()
            )

            authorization_url = (
                build_line_authorization_url(
                    state=state,
                    callback_url=deps.oauth_callback_url(
                        request,
                        "line",
                    ),
                )
            )

            response = RedirectResponse(
                url=authorization_url,
                status_code=302,
            )

            # OAuth state 暫存在 HttpOnly cookie。
            # LINE callback 回來後進行比對。
            response.set_cookie(
                key=
                    "blink_line_oauth_state",

                value=
                    state,

                httponly=
                    True,

                secure=
                    onboarding_cookie_secure(
                        request.url.hostname
                        or ""
                    ),

                samesite=
                    "lax",

                max_age=
                    600,

                path=
                    "/",
            )

            # 如果是「綁定 LINE」而不是登入，
            # 額外紀錄目前正在綁定 LINE。
            if link_mode:
                response.set_cookie(
                    key=
                        OAUTH_LINK_COOKIE_NAME,

                    value=
                        "line",

                    max_age=
                        600,

                    httponly=
                        True,

                    secure=
                        onboarding_cookie_secure(
                            request.url.hostname
                            or ""
                        ),

                    samesite=
                        onboarding_cookie_samesite(
                            request.url.hostname
                            or ""
                        ),

                    path=
                        "/",
                )
            else:
                # 一般 LINE 登入必須清除先前失敗的帳號綁定狀態。
                # 否則 callback 會被舊 cookie 誤判為 mode=link。
                response.delete_cookie(
                    key=
                        OAUTH_LINK_COOKIE_NAME,

                    path=
                        "/",
                )

            return response

        except HTTPException:
            raise

        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "啟動 LINE Login 失敗："
                    + str(
                        error
                    )
                ),
            ) from error


    @router.get("/api/auth/line/callback")
    def line_login_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        """
        LINE Login 完成後 callback。

        流程：

        1. 檢查 LINE 是否回傳錯誤
        2. 驗證 OAuth state
        3. authorization code 換 token
        4. access_token 取得 LINE Profile
        5. 驗證 id_token
        6. 從 id_token 取得 Email
        7. 建立 / 更新 BLINK AI 帳號
        8. 建立登入 Session
        9. Redirect 回前端
        """

        # -----------------------------------------------------
        # LINE 回傳 OAuth error
        # -----------------------------------------------------

        if error:
            raise HTTPException(
                status_code=400,
                detail=(
                    "LINE Login 被取消或失敗："
                    + (
                        error_description
                        or error
                    )
                ),
            )


        # -----------------------------------------------------
        # Authorization Code
        # -----------------------------------------------------

        if not code:
            raise HTTPException(
                status_code=400,
                detail=
                    "LINE callback 缺少 code",
            )


        # -----------------------------------------------------
        # OAuth State
        # -----------------------------------------------------

        if not state:
            raise HTTPException(
                status_code=400,
                detail=
                    "LINE callback 缺少 state",
            )

        expected_state = (
            request.cookies.get(
                "blink_line_oauth_state"
            )
        )

        if (
            not expected_state
            or not secrets.compare_digest(
                expected_state,
                state,
            )
        ):
            raise HTTPException(
                status_code=400,
                detail=
                    "LINE Login state 驗證失敗",
            )


        try:
            # =================================================
            # 1. authorization code → token
            # =================================================

            token_result = (
                exchange_line_code(
                    code=code,
                    callback_url=deps.oauth_callback_url(
                        request,
                        "line",
                    ),
                )
            )


            access_token = str(
                token_result.get(
                    "access_token"
                )
                or ""
            ).strip()

            if not access_token:
                raise RuntimeError(
                    "LINE 未回傳 access_token"
                )


            id_token = str(
                token_result.get(
                    "id_token"
                )
                or ""
            ).strip()

            if not id_token:
                raise RuntimeError(
                    "LINE 未回傳 id_token"
                )


            # =================================================
            # 2. access_token → LINE Profile
            # =================================================

            profile = (
                get_line_profile(
                    access_token=
                        access_token
                )
            )


            line_user_id = str(
                profile.get(
                    "userId"
                )
                or ""
            ).strip()

            if not line_user_id:
                raise RuntimeError(
                    "LINE Profile 缺少 userId"
                )


            # =================================================
            # 3. 驗證 LINE ID Token
            #
            # LINE 官方 Verify API 會驗證：
            # - token
            # - issuer
            # - audience / Channel ID
            # - expiration
            #
            # 並使用 user_id 確認 sub
            # 與 Profile API 的 userId 相同。
            # =================================================

            id_token_payload = (
                verify_line_id_token(
                    id_token=
                        id_token,

                    user_id=
                        line_user_id,
                )
            )


            # 再做一次 server-side 保護。
            id_token_subject = str(
                id_token_payload.get(
                    "sub"
                )
                or ""
            ).strip()

            if (
                not id_token_subject
                or id_token_subject
                != line_user_id
            ):
                raise RuntimeError(
                    "LINE ID token 使用者識別不一致"
                )


            # =================================================
            # 4. Display Name
            # =================================================

            display_name = str(
                profile.get(
                    "displayName"
                )
                or id_token_payload.get(
                    "name"
                )
                or "LINE 使用者"
            ).strip()


            # =================================================
            # 5. Email
            #
            # Email 不從 /v2/profile 取得。
            # Email 來自已驗證的 ID token。
            # =================================================

            email = str(
                id_token_payload.get(
                    "email"
                )
                or ""
            ).strip().lower()


            # LINE 沒有 Google 那種 email_verified claim。
            #
            # 這裡的 email 是由：
            # 1. LINE Email permission
            # 2. LINE Login
            # 3. LINE 官方 ID token verify endpoint
            #
            # 驗證後取得。
            #
            # 在 BLINK AI 帳號系統中，
            # 有成功取得 Email 即視為 OAuth provider
            # 提供的可信 Email。
            email_verified = bool(
                email
            )


            # =================================================
            # 6. 建立 BLINK User ID
            # =================================================

            proposed_user_id = (
                create_blink_user_id_from_line(
                    line_user_id=
                        line_user_id
                )
            )


            # =================================================
            # 7. Link Mode / Login Mode
            # =================================================

            link_mode = (
                request.cookies.get(
                    OAUTH_LINK_COOKIE_NAME
                )
                == "line"
            )

            session_account = None

            if link_mode:
                try:
                    session_account = (
                        deps.require_auth_session(
                            request
                        )
                    )

                except HTTPException as session_error:
                    if session_error.status_code not in {
                        401,
                        403,
                    }:
                        raise

                    # 綁定 cookie 尚未消失，但原本的登入 session 已失效。
                    # 改按一般 LINE 登入處理，避免持續被送回 profile。
                    link_mode = False


            if link_mode:
                # ---------------------------------------------
                # 已登入會員：
                # 把 LINE identity 綁到現有 BLINK AI account
                # ---------------------------------------------

                account = (
                    create_password_auth_service()
                    .link_oauth_identity(
                        user_id=
                            session_account[
                                "user_id"
                            ],

                        provider=
                            "line",

                        provider_subject=
                            line_user_id,

                        display_name=
                            display_name,

                        email=
                            email,

                        # 一般登入只以 LINE provider_subject 辨識帳號。
                        # Email 保留為 provider 資料，但不自動合併既有會員；
                        # 帳號合併必須由已登入使用者在基本資料頁主動綁定。
                        email_verified=
                            False,
                    )
                )

                frontend_url = (
                    deps.frontend_profile_url(
                        "line",
                        request=request,
                    )
                )

                onboarding_token = ""

            else:
                # ---------------------------------------------
                # LINE 快速登入：
                # 建立或更新 LINE OAuth account
                # ---------------------------------------------

                account = (
                    create_password_auth_service()
                    .ensure_oauth_user(
                        user_id=
                            proposed_user_id,

                        provider=
                            "line",

                        provider_subject=
                            line_user_id,

                        display_name=
                            display_name,

                        email=
                            email,

                        email_verified=
                            email_verified,
                    )
                )


                onboarding_token = (
                    issue_onboarding_token(
                        user_id=
                            account[
                                "user_id"
                            ],

                        provider=
                            "line",
                    )
                )


                frontend_url = (
                    deps.oauth_success_url(
                        request,
                        "line",
                    )
                )


            # =================================================
            # 8. Redirect
            # =================================================

            response = RedirectResponse(
                url=
                    frontend_url,

                status_code=
                    302,
            )


            # =================================================
            # 9. 建立 Login Session
            # =================================================

            if not link_mode:
                deps.issue_auth_session(
                    request=
                        request,

                    response=
                        response,

                    user_id=
                        account[
                            "user_id"
                        ],

                    auth_method=
                        "line",
                )


                response.set_cookie(
                    key=
                        ONBOARDING_COOKIE_NAME,

                    value=
                        onboarding_token,

                    max_age=
                        ONBOARDING_TOKEN_TTL_SECONDS,

                    httponly=
                        True,

                    secure=
                        onboarding_cookie_secure(
                            request.url.hostname
                            or ""
                        ),

                    samesite=
                        onboarding_cookie_samesite(
                            request.url.hostname
                            or ""
                        ),

                    path=
                        "/",
                )


            # =================================================
            # 10. 清除 OAuth 暫存 Cookie
            # =================================================

            response.delete_cookie(
                key=
                    OAUTH_LINK_COOKIE_NAME,

                path=
                    "/",
            )


            response.delete_cookie(
                key=
                    "blink_line_oauth_state",

                path=
                    "/",
            )


            return response


        # -----------------------------------------------------
        # OAuth Identity 衝突
        # -----------------------------------------------------

        except (
            OAuthIdentityAlreadyLinkedError,
            EmailIdentityConflictError,
        ) as oauth_error:

            print(
                "[LINE OAuth conflict]"
                f" link_mode={link_mode}"
                f" error={oauth_error!r}",
                flush=True,
            )

            response = RedirectResponse(
                url=deps.frontend_profile_url(
                    "line",
                    request=request,
                    error=str(
                        oauth_error
                    ),
                ),
                status_code=302,
            )

            response.delete_cookie(
                key=
                    OAUTH_LINK_COOKIE_NAME,

                path=
                    "/",
            )

            response.delete_cookie(
                key=
                    "blink_line_oauth_state",

                path=
                    "/",
            )

            return response


        except HTTPException:
            raise


        except Exception as callback_error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "LINE Login callback 處理失敗："
                    + str(
                        callback_error
                    )
                ),
            ) from callback_error


    @router.get("/api/auth/google/login")
    def google_login(request: Request):
        """
        開始 Google OAuth Login。
        """

        # The Google client already authorizes the localhost:8010 callback.
        # When the request arrives through the Next.js loopback proxy, first
        # move the browser to the same localhost cookie domain. This keeps the
        # OAuth state cookie available at the callback without weakening CSRF
        # state validation or requiring another redirect URI in Google Cloud.
        forwarded_host = (request.headers.get("x-forwarded-host") or "").strip()
        if forwarded_host and (request.url.hostname or "").lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            suffix = "?mode=link" if request.query_params.get("mode") == "link" else ""
            return RedirectResponse(
                url=f"http://localhost:8010/api/auth/google/login{suffix}",
                status_code=307,
            )

        try:
            link_mode = request.query_params.get("mode") == "link"
            if link_mode:
                deps.require_auth_session(request)

            state = create_google_state()

            authorization_url = (
                build_google_authorization_url(
                    state=state,
                    callback_url=deps.oauth_callback_url(request, "google"),
                )
            )

            response = RedirectResponse(
                url=authorization_url,
                status_code=302,
            )

            # 儲存 OAuth state。
            #
            # localhost 開發環境使用 secure=False。
            # 正式 HTTPS 上線後應改成 secure=True。
            response.set_cookie(
                key="google_oauth_state",
                value=state,
                httponly=True,
                secure=onboarding_cookie_secure(request.url.hostname or ""),
                samesite="lax",
                max_age=600,
                path="/",
            )

            if link_mode:
                response.set_cookie(
                    key=OAUTH_LINK_COOKIE_NAME,
                    value="google",
                    max_age=600,
                    httponly=True,
                    secure=onboarding_cookie_secure(request.url.hostname or ""),
                    samesite=onboarding_cookie_samesite(request.url.hostname or ""),
                    path="/",
                )

            return response

        except HTTPException:
            raise

        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "啟動 Google Login 失敗："
                    f"{error}"
                ),
            )


    @router.get("/api/auth/google/callback")
    def google_login_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        """
        Google OAuth callback。

        流程：
        1. 檢查 Google 是否回傳錯誤
        2. 驗證 state
        3. authorization code 換 access token
        4. 取得 Google UserInfo
        5. 建立 BLINK user_id
        6. Redirect 回前端
        """

        if error:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google Login 被取消或失敗："
                    + (
                        error_description
                        or error
                    )
                ),
            )

        if not code:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google callback "
                    "缺少 authorization code"
                ),
            )

        cookie_state = request.cookies.get(
            "google_oauth_state"
        )

        if (
            not cookie_state
            or not state
            or not secrets.compare_digest(
                cookie_state,
                state,
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Google OAuth state 驗證失敗",
            )

        try:
            token_result = exchange_google_code(
                code=code,
                callback_url=deps.oauth_callback_url(request, "google"),
            )

            access_token = token_result.get(
                "access_token"
            )

            if not access_token:
                raise RuntimeError(
                    "Google 沒有回傳 access_token"
                )

            profile = get_google_profile(
                access_token=access_token
            )

            google_user_id = profile.get("sub")

            if not google_user_id:
                raise RuntimeError(
                    "Google UserInfo 缺少 sub"
                )

            proposed_user_id = (
                create_blink_user_id_from_google(
                    google_user_id
                )
            )

            display_name = (
                profile.get("name")
                or profile.get("email")
                or "Google 使用者"
            )

            email = (
                profile.get("email")
                or ""
            )
            email_verified = bool(profile.get("email_verified"))
            link_mode = request.cookies.get(OAUTH_LINK_COOKIE_NAME) == "google"
            if link_mode:
                session_account = deps.require_auth_session(request)
                account = create_password_auth_service().link_oauth_identity(
                    user_id=session_account["user_id"],
                    provider="google",
                    provider_subject=google_user_id,
                    display_name=display_name,
                    email=email,
                    email_verified=email_verified,
                )
                frontend_url = deps.frontend_profile_url("google", request=request)
                onboarding_token = ""
            else:
                account = create_password_auth_service().ensure_oauth_user(
                    user_id=proposed_user_id,
                    provider="google",
                    provider_subject=google_user_id,
                    display_name=display_name,
                    email=email,
                    email_verified=email_verified,
                )
                onboarding_token = issue_onboarding_token(
                    user_id=account["user_id"],
                    provider="google",
                )
                frontend_url = deps.oauth_success_url(request, "google")

            response = RedirectResponse(
                url=frontend_url,
                status_code=302,
            )

            if not link_mode:
                deps.issue_auth_session(
                    request=request,
                    response=response,
                    user_id=account["user_id"],
                    auth_method="google",
                )

                response.set_cookie(
                    key=ONBOARDING_COOKIE_NAME,
                    value=onboarding_token,
                    max_age=ONBOARDING_TOKEN_TTL_SECONDS,
                    httponly=True,
                    secure=onboarding_cookie_secure(request.url.hostname or ""),
                    samesite=onboarding_cookie_samesite(request.url.hostname or ""),
                    path="/",
                )

            response.delete_cookie(key=OAUTH_LINK_COOKIE_NAME, path="/")

            response.delete_cookie(
                key="google_oauth_state",
                path="/",
            )

            return response

        except (OAuthIdentityAlreadyLinkedError, EmailIdentityConflictError) as error:
            return RedirectResponse(
                url=deps.frontend_profile_url("google", request=request, error=str(error)),
                status_code=302,
            )

        except HTTPException:
            raise

        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Google Login callback 失敗："
                    f"{error}"
                ),
            )


    return router
