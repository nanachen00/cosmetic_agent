"""BLINK AI 後端 API — FastAPI 包裝 MAS（第一段：welcome/chat/session）"""
import os
import sys
import asyncio
import json
import time
import logging
import hmac
import ipaddress
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')
import hashlib
import secrets
from threading import Lock
from uuid import UUID, uuid4
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Optional, Dict

from agents.root.orchestrator import RootAgentOrchestrator
from app.security.request_guard import FixedWindowLimiter, RateLimitRule
from app.security.chat_guard import (
    ChatConcurrentRequest,
    ChatGuard,
    ChatGuardUnavailable,
    ChatRateLimitExceeded,
    DuplicateChatRequest,
    GuestSigner,
    estimate_tokens,
    hash_payload,
    valid_request_id,
)
from app.security.conversation_limit import (
    ConversationLimitPolicy,
    conversation_limit_status,
)
from core.admission import AdmissionFull, AdmissionTimeout, ChatAdmissionController
from core.observability import install_observability
from core.observability import (
    observe_chat_overload,
    observe_pipeline_stage,
    set_chat_admission_state,
)

from tools.auth.line_login import (
    create_line_state,
    build_line_authorization_url,
    exchange_line_code,
    verify_line_id_token,
    get_line_profile,
    create_blink_user_id_from_line,
    build_frontend_success_url,
)

from tools.auth.google_login import (
    create_google_state,
    build_google_authorization_url,
    exchange_google_code,
    get_google_profile,
    create_blink_user_id_from_google,
    build_frontend_google_success_url,
)
from tools.auth.password_login import (
    AccountNotFoundError,
    InvalidCredentialsError,
    LastLoginMethodError,
    LoginRateLimitedError,
    EmailIdentityConflictError,
    OAuthIdentityAlreadyLinkedError,
    PasswordAlreadySetError,
    PasswordAuthError,
    PasswordNotSetError,
    PasswordValidationError,
    UsernameAlreadyExistsError,
    UsernameAlreadySetError,
    UsernameChangeCooldownError,
    UsernameValidationError,
    create_password_auth_service,
)
from tools.auth.auth_session import (
    SESSION_COOKIE_NAME,
    AuthSessionError,
    InvalidSessionError,
    RecentAuthenticationRequiredError,
    create_auth_session_service,
    session_cookie_options,
)
from tools.auth.oauth_onboarding import (
    ONBOARDING_COOKIE_NAME,
    ONBOARDING_TOKEN_TTL_SECONDS,
    OnboardingTokenError,
    build_onboarding_url,
    issue_onboarding_token,
    onboarding_cookie_samesite,
    onboarding_cookie_secure,
    verify_onboarding_token,
)
from tools.auth.password_reset import (
    InvalidPasswordResetCodeError,
    PasswordResetError,
    PasswordResetNotConfiguredError,
    PasswordResetRateLimitedError,
    VerifiedEmailRequiredError,
    create_password_reset_service,
)

# ── skin_tracking 新架構組裝（同學的 composition 層）──
try:
    from agents.skin_tracking.composition import build_skin_tracking_from_env
    from agents.skin_tracking import tracking_agent as _skin_ta
    _skin_components = build_skin_tracking_from_env(
        env_file='/var/lib/jenkins/tomod/cosmetic-agent/.env')
    _skin_ta.configure(_skin_components.extractor, _skin_components.store)
    print("[INIT] skin_tracking 組裝完成（composition 層）")
except Exception as _e:
    print(f"[WARNING] skin_tracking 組裝失敗（膚況功能將不可用）：{_e}")
from agents.skin_tracking.fastapi_integration import install_skin_tracking
from tools.user.quiz_analysis_tool import (
    get_quiz_questions,

    record_quiz_result,
    get_latest_quiz_result,
)
from app.api.routes.quiz import create_quiz_router

app = FastAPI(title="BLINK AI MAS API", version="0.1")
_CHAT_SECURITY_LOGGER = logging.getLogger("blink.chat_security")
_CHAT_GUARD = ChatGuard()
_CHAT_SECURITY_LOG_LIMITER = FixedWindowLimiter(max_buckets=10_000)
_CHAT_SECURITY_LOG_HMAC_SECRET = (
    os.getenv("CHAT_SECURITY_LOG_HMAC_SECRET", "").strip().encode("utf-8")
    or os.getenv("CHAT_GUEST_SESSION_SECRET", "").strip().encode("utf-8")
    or secrets.token_bytes(32)
)
_GUEST_COOKIE_NAME = "blink_guest_session"
_GUEST_COOKIE_TTL_SECONDS = max(
    3600,
    int(os.getenv("CHAT_GUEST_SESSION_TTL_SECONDS", str(30 * 24 * 3600))),
)
_GUEST_SIGNER = None
_CONVERSATION_LIMIT_POLICY = ConversationLimitPolicy.from_env()


async def _read_conversation_limit_history(agent) -> list:
    try:
        history = await run_in_threadpool(agent.memory.get_history, lastn=1_000)
        return list(history or [])
    except Exception as exc:
        _CHAT_SECURITY_LOGGER.exception("conversation_limit_history_failed")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "conversation_limit_unavailable",
                "message": "暫時無法確認對話容量，請稍後再試。",
            },
        ) from exc


async def _enforce_conversation_limit(
    *,
    agent,
    request: Request,
    user_id: str,
    incoming_message: str,
) -> list:
    del request, user_id
    history = await _read_conversation_limit_history(agent)
    current = conversation_limit_status(
        history,
        policy=_CONVERSATION_LIMIT_POLICY,
    )
    projected = conversation_limit_status(
        history,
        policy=_CONVERSATION_LIMIT_POLICY,
        incoming_user_text=incoming_message,
    )
    would_exceed = (
        current["blocked"]
        or projected["user_messages"] > _CONVERSATION_LIMIT_POLICY.max_user_messages
        or projected["used_tokens"] > _CONVERSATION_LIMIT_POLICY.max_tokens
    )
    if would_exceed:
        status = current if current["blocked"] else projected
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conversation_limit_reached",
                "message": "對話內容已額滿，請開新對話。",
                "conversation_limit": status,
            },
        )
    return history


def _completed_conversation_limit(
    history: list,
    incoming_message: str,
    result: dict,
) -> dict:
    assistant_text = ""
    for key in ("reply", "message", "content", "text"):
        value = result.get(key)
        if isinstance(value, str):
            assistant_text = value
            break
    return conversation_limit_status(
        history,
        policy=_CONVERSATION_LIMIT_POLICY,
        incoming_user_text=incoming_message,
        incoming_assistant_text=assistant_text,
    )
SESSION_TRUSTED_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:13001", "http://127.0.0.1:13001", "https://blinkai.tw", "https://www.blinkai.tw"}
@app.middleware("http")
async def reject_cross_site_session_requests(request: Request, call_next):
    return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked"}) if (request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get(SESSION_COOKIE_NAME) and (request.headers.get("sec-fetch-site", "").lower() == "cross-site" or ((origin := request.headers.get("origin", "").rstrip("/")) and origin not in SESSION_TRUSTED_ORIGINS))) else await call_next(request)


_API_RATE_LIMITER = FixedWindowLimiter()
_API_RATE_LIMITS = {
    ("POST", "/api/v1/chat"): RateLimitRule(30, 60),
    ("POST", "/api/chat/message"): RateLimitRule(30, 60),
    ("POST", "/api/chat/message/stream"): RateLimitRule(30, 60),
    ("POST", "/api/conversations"): RateLimitRule(10, 600),
    ("POST", "/api/skin-records/frontend"): RateLimitRule(20, 60),
    ("POST", "/api/checkin"): RateLimitRule(10, 60),
    ("POST", "/api/quiz/submit"): RateLimitRule(10, 60),
    ("POST", "/api/auth/password/login"): RateLimitRule(30, 300),
    ("POST", "/api/auth/password/register"): RateLimitRule(20, 600),
    ("POST", "/api/auth/password/reset/request"): RateLimitRule(20, 600),
    ("POST", "/api/auth/password/recovery/request"): RateLimitRule(20, 600),
    ("POST", "/api/auth/guest-session"): RateLimitRule(20, 600),
}
_MAX_API_BODY_BYTES = 2 * 1024 * 1024
_SENSITIVE_API_PREFIXES = (
    "/api/auth",
    "/api/user",
    "/api/conversations",
    "/api/chat",
    "/api/v1/chat",
    "/api/skin",
    "/api/checkin",
)


def _request_limit_identity(request: Request) -> str:
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if session_token:
        digest = hashlib.sha256(session_token.encode("utf-8")).hexdigest()[:24]
        return f"session:{digest}"
    return f"client:{request.client.host if request.client else 'unknown'}"


@app.middleware("http")
async def enforce_api_request_limits(request: Request, call_next):
    content_length = 0
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH"}:
        raw_length = request.headers.get("content-length", "").strip()
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid request size"})
    if content_length > _MAX_API_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Request body too large"})


    if rule is not None:
        identity = _request_limit_identity(request)
        bucket = "ai-chat" if request.url.path in {"/api/v1/chat", "/api/chat/message"} else request.url.path
        retry_after = _API_RATE_LIMITER.check(
            f"{request.method}:{bucket}:{identity}",
            rule,
        )
        if retry_after:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )

    response = await call_next(request)
    if request.url.path.startswith(_SENSITIVE_API_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if "x-content-type-options" not in response.headers:
        response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    # Browsers ignore HSTS received over plain HTTP. Sending it on every API
    # response avoids losing the header when a trusted proxy terminates TLS.
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=63072000; includeSubDomains; preload",
    )
    return response



def _resolve_skin_tracking_user_id(request: Request) -> str:
    """優先使用登入 session；開發版亦相容前端保存的會員識別。"""
    requested_user_id = request.query_params.get("user_id", "").strip()
    user_id = _current_skin_user_id(request, requested_user_id or "anonymous")
    if user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id


install_skin_tracking(app, get_user_id=_resolve_skin_tracking_user_id)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:13001",
        "http://127.0.0.1:13001",
        "https://blinkai.tw",
        "https://www.blinkai.tw",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
_SESSION_CACHE_MAX = max(1, int(os.getenv("SESSION_CACHE_MAX", "512")))
_sessions = {}
_sessions_lock = Lock()

_CHAT_QUEUE_TIMEOUT_SECONDS = max(
    0.1,
    float(os.getenv("CHAT_QUEUE_TIMEOUT_SECONDS", "45")),
)
_CHAT_ADMISSION = ChatAdmissionController(
    max_running=int(os.getenv("CHAT_MAX_RUNNING", "2")),
    max_queued=int(os.getenv("CHAT_MAX_QUEUED", "8")),
    on_state=set_chat_admission_state,
    on_overload=observe_chat_overload,
)


def _postgres_ready() -> bool:
    from database.postgres.connection import check_postgres_connection

    check_postgres_connection()
    return True


def _redis_ready() -> bool:
    from redis import Redis

    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return False
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


def _internal_http_ready(url: str) -> bool:
    from urllib.request import urlopen

    with urlopen(url, timeout=1.5) as response:
        return response.status == 200


_READINESS_CHECKS = {
    "postgres": _postgres_ready,
    "redis": _redis_ready,
    "taide": lambda: _internal_http_ready(
        os.getenv("TAIDE_HEALTH_URL", "http://127.0.0.1:8000/healthz")
    ),
    "taide_router": lambda: _internal_http_ready(
        os.getenv("TAIDE_ROUTER_HEALTH_URL", "http://127.0.0.1:8001/healthz")
    ),
    "embedding": lambda: _internal_http_ready(
        os.getenv("EMBEDDING_HEALTH_URL", "http://127.0.0.1:8002/healthz")
    ),
}







install_observability(
    app,
    session_count_provider=lambda: len(_sessions),
    readiness_checks=_READINESS_CHECKS,
)


def normalize_conversation_id(
    conversation_id: Optional[str],
) -> str:
    value = (
        conversation_id
        or "default"
    ).strip()

    return value or "default"


def get_agent(
    user_id: str,
    conversation_id: str = "default",
) -> RootAgentOrchestrator:
    conversation_id = (
        normalize_conversation_id(
            conversation_id
        )
    )

    session_key = (
        user_id,
        conversation_id,
    )

    with _sessions_lock:
        agent = _sessions.pop(session_key, None)
        if agent is None:
            agent = RootAgentOrchestrator(
                user_id=user_id,
                conversation_id=conversation_id,
            )
        _sessions[session_key] = agent

        while len(_sessions) > _SESSION_CACHE_MAX:
            _sessions.pop(next(iter(_sessions)))

        return agent
def get_agent_with_profile(
    user_id: str,
    conversation_id: str = "default",
) -> RootAgentOrchestrator:
    """
    取得會在每輪 process() 前重新載入 Profile 的 Agent。

    Profile 的讀取、正規化與聊天同步統一由 Root Agent 的
    ProfileContextBridge 處理，避免 API 與 Agent 各讀一次資料庫。
    """
    if (
        not user_id
        or user_id == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少 user_id 或 session_id",
        )

    return get_agent(
        user_id=user_id,
        conversation_id=
            conversation_id,
    )


class ChatRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)
    conversation_id: Optional[str] = Field(default="default", max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    flow: Optional[str] = Field(default=None, max_length=64)

    @property
    def uid(self) -> str:
        return (
            self.user_id
            or self.session_id
            or "anonymous"
        )

    @property
    def cid(self) -> str:
        return normalize_conversation_id(
            self.conversation_id
        )


class ConversationCreateRequest(
    BaseModel
):
    user_id: str


def _chat_user_id(request: Request, proposed_user_id: str = "anonymous") -> str:
    token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if token:
        return str(_require_auth_session(request)["user_id"])
    guest_id = _guest_signer().verify(
        request.cookies.get(_GUEST_COOKIE_NAME, ""),
    )
    if not guest_id:
        raise HTTPException(status_code=401, detail="Guest session required")
    # Never authorize guest access from a body/query user_id.  The signed,
    # HttpOnly cookie is the sole source of truth and prevents guest IDOR.
    return guest_id


def _guest_signer() -> GuestSigner:
    global _GUEST_SIGNER
    if _GUEST_SIGNER is None:
        source_secret = (
            os.getenv("CHAT_GUEST_SESSION_SECRET", "").strip()
            or os.getenv("PASSWORD_RESET_SECRET", "").strip()
        )
        if len(source_secret) < 24:
            raise HTTPException(
                status_code=503,
                detail="Guest sessions are not configured",
            )
        derived_secret = hmac.new(
            source_secret.encode("utf-8"),
            b"blink-guest-session-v1",
            hashlib.sha256,
        ).hexdigest()
        _GUEST_SIGNER = GuestSigner(
            secret=derived_secret,
            ttl_seconds=_GUEST_COOKIE_TTL_SECONDS,
        )
    return _GUEST_SIGNER


@app.post("/api/auth/guest-session")
def issue_guest_session(request: Request, response: Response):
    """Issue a server-bound guest identity; never trust a browser-generated ID."""
    signer = _guest_signer()
    cookie = request.cookies.get(_GUEST_COOKIE_NAME, "")
    guest_id = signer.verify(cookie)
    if not guest_id:
        guest_id, cookie = signer.issue()
        hostname = (request.url.hostname or "").lower()
        response.set_cookie(
            key=_GUEST_COOKIE_NAME,
            value=cookie,
            max_age=_GUEST_COOKIE_TTL_SECONDS,
            httponly=True,
            secure=hostname not in {"localhost", "127.0.0.1"},
            samesite="lax",
            path="/",
        )
    response.headers["Cache-Control"] = "no-store"
    return {"user_id": guest_id}


def _chat_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else "unknown"
    # Only enable after the origin accepts traffic exclusively from a trusted
    # reverse proxy which overwrites X-Real-IP.  Never trust CF headers merely
    # because CF-Ray is present; both can be forged on a public origin.
    if os.getenv("CHAT_TRUST_PROXY_HEADERS", "0").strip().lower() not in {"1", "true", "yes"}:
        return direct_ip
    forwarded = request.headers.get("x-real-ip", "").strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return direct_ip


def _security_hash(value: str) -> str:
    return hmac.new(
        _CHAT_SECURITY_LOG_HMAC_SECRET,
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def _log_chat_block(request: Request, user_id: str, reason: str) -> None:
    account_hash = _security_hash(user_id)
    ip_hash = _security_hash(_chat_client_ip(request))
    if _CHAT_SECURITY_LOG_LIMITER.check(
        "chat_security:global",
        RateLimitRule(300, 60),
    ):
        return
    if _CHAT_SECURITY_LOG_LIMITER.check(
        f"{request.url.path}:{reason}:{account_hash}:{ip_hash}",
        RateLimitRule(1, 60),
    ):
        return
    _CHAT_SECURITY_LOGGER.warning(
        "chat_request_blocked route=%s reason=%s account_hash=%s ip_hash=%s",
        request.url.path,
        reason,
        account_hash,
        ip_hash,
    )


async def _begin_chat_security(
    request: Request,
    req: ChatRequest,
    user_id: str,
):
    request_id = request.headers.get("idempotency-key", "").strip()
    if not valid_request_id(request_id):
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header is required",
        )
    message = req.message.strip()
    if not message or any(ord(character) < 32 and character not in "\n\r\t" for character in message):
        raise HTTPException(status_code=422, detail="Message contains unsupported control characters")
    input_tokens = estimate_tokens(message)
    max_input_tokens = max(128, int(os.getenv("CHAT_MAX_INPUT_TOKENS", "2000")))
    if input_tokens > max_input_tokens:
        raise HTTPException(status_code=413, detail="Message is too long")
    try:
        return await _CHAT_GUARD.begin(
            user_id=user_id,
            ip=_chat_client_ip(request),
            request_id=request_id,
            route=request.url.path,
            conversation_id=req.cid,
            payload_hash=hash_payload(message, req.flow),
            is_guest=user_id.startswith("guest-"),
            input_tokens=input_tokens,
        )
    except ChatRateLimitExceeded as error:
        _log_chat_block(request, user_id, error.scope)
        raise HTTPException(
            status_code=429,
            detail="Usage limit reached; please try again later",
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        ) from error
    except ChatConcurrentRequest as error:
        _log_chat_block(request, user_id, "account_concurrency")
        raise HTTPException(
            status_code=429,
            detail="A reply is already being generated for this account",
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        ) from error
    except DuplicateChatRequest as error:
        _log_chat_block(request, user_id, "idempotency_payload_mismatch" if error.different_payload else "duplicate_request")
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with different content" if error.different_payload else "This message request was already accepted",
            headers={"Cache-Control": "no-store"},
        ) from error
    except ChatGuardUnavailable as error:
        _log_chat_block(request, user_id, "guard_unavailable")
        raise HTTPException(
            status_code=503,
            detail="Chat protection is temporarily unavailable",
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        ) from error

async def _guard_conversation_creation(request: Request, user_id: str) -> None:
    try:
        await _CHAT_GUARD.check_conversation_create(
            user_id=user_id,
            ip=_chat_client_ip(request),
        )
    except ChatRateLimitExceeded as error:
        _log_chat_block(request, user_id, error.scope)
        raise HTTPException(
            status_code=429,
            detail="Too many new conversations; please try again later",
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        ) from error
    except ChatGuardUnavailable as error:
        _log_chat_block(request, user_id, "conversation_create_guard_unavailable")
        raise HTTPException(
            status_code=503,
            detail="Conversation protection is temporarily unavailable",
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        ) from error


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _issue_auth_session(
    *,
    request: Request,
    response: Response,
    user_id: str,
    auth_method: str,
) -> None:
    token = create_auth_session_service().create(
        user_id=user_id,
        auth_method=auth_method,
        client_ip=_client_host(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        **session_cookie_options(request.url.hostname or ""),
    )


def _require_auth_session(
    request: Request,
    *,
    require_recent: bool = False,
):
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        return create_auth_session_service().authenticate(
            token,
            require_recent=require_recent,
        )
    except RecentAuthenticationRequiredError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except InvalidSessionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except AuthSessionError as error:
        raise HTTPException(status_code=500, detail="登入狀態服務暫時無法使用") from error


OAUTH_LINK_COOKIE_NAME = "blink_oauth_link_provider"


def _is_local_oauth_request(request: Request) -> bool:
    return (request.url.hostname or "").lower() in {
        "localhost",
        "127.0.0.1",
    }


def _is_loopback_url(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "").lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    except ValueError:
        return False


def _public_api_url(path: str) -> str:
    origin = os.getenv("PUBLIC_API_ORIGIN", "https://api.blinkai.tw").strip().rstrip("/")
    return f"{origin}/{path.lstrip('/')}"


def _public_frontend_url(path: str) -> str:
    origin = os.getenv(
        "PUBLIC_FRONTEND_ORIGIN",
        "https://www.blinkai.tw",
    ).strip().rstrip("/")
    return f"{origin}/{path.lstrip('/')}"


def _public_or_fallback(configured: str, fallback: str) -> str:
    value = (configured or "").strip()
    return fallback if not value or _is_loopback_url(value) else value


def _local_frontend_url(path: str) -> str:
    origin = os.getenv(
        "LOCAL_FRONTEND_ORIGIN",
        "http://localhost:3000",
    ).strip().rstrip("/")
    return f"{origin}/{path.lstrip('/')}"


def _oauth_callback_url(request: Request, provider: str) -> str:
    if _is_local_oauth_request(request):
        local_api_origin = os.getenv(
            "LOCAL_API_ORIGIN",
            "http://localhost:8011",
        ).strip().rstrip("/")
        return f"{local_api_origin}/api/auth/{provider}/callback"
    return _public_or_fallback(
        os.getenv(f"{provider.upper()}_CALLBACK_URL", ""),
        _public_api_url(f"/api/auth/{provider}/callback"),
    )


def _oauth_success_url(request: Request, provider: str) -> str:
    if _is_local_oauth_request(request):
        return _local_frontend_url(f"/login/{provider}-success")
    return _public_or_fallback(
        build_onboarding_url(provider),
        _public_frontend_url(f"/login/{provider}-success"),
    )


def _frontend_profile_url(
    provider: str,
    *,
    request: Optional[Request] = None,
    error: str = "",
) -> str:
    if request is not None and _is_local_oauth_request(request):
        base_url = _local_frontend_url("/profile")
    else:
        configured = os.getenv("FRONTEND_PROFILE_URL", "").strip()
        if configured:
            base_url = _public_or_fallback(
                configured,
                _public_frontend_url("/profile"),
            )
        else:
            onboarding_url = os.getenv(
                "FRONTEND_ONBOARDING_URL",
                "",
            ).strip()
            if onboarding_url and not _is_loopback_url(onboarding_url):
                parsed = urlsplit(onboarding_url)
                base_url = urlunsplit((parsed.scheme, parsed.netloc, "/profile", "", ""))
            else:
                base_url = _public_frontend_url("/profile")
    query = {"linked": provider}
    if error:
        query = {"link_error": "oauth_link_failed"}
    return f"{base_url}?{urlencode(query)}"


def _stage_to_route(stage: str) -> str:
    """把後端 stage 映射成前端的 RootRoute"""
    if stage.startswith("skin_tracking"):
        return "skin_record"
    if stage.startswith(
        (
            "clarification",
            "pain_gain",
            "route_clarification",
            "multi_intent",
        )
    ):
        return "clarify"
    if stage.startswith("guarded"):
        return "clarify"
    return "product_search"


from app.api.routes.health import create_health_router
app.include_router(create_health_router(lambda: len(_sessions)))

from app.api.routes.daily_content import router as daily_content_router
from app.api.routes.admin import create_admin_router
app.include_router(daily_content_router)
app.include_router(create_admin_router(require_auth_session=_require_auth_session, session_count=lambda: len(_sessions)))


@app.get("/api/v1/welcome")
def welcome(request: Request, user_id: str = "anonymous"):
    owner_id = _chat_user_id(request, user_id)
    return get_agent(owner_id).get_welcome()


from app.api.routes.auth import (
    router as auth_router,
    register_onboarding_status_route,
    register_password_entry_routes,
    register_password_management_routes,
    register_provider_management_routes,
    register_session_routes,
    register_username_routes,
)
from app.api.routes.password_recovery import (
    register_password_recovery_routes,
    router as password_recovery_router,
)
from app.api.routes.email_registration import (
    register_email_registration_routes,
    router as email_registration_router,
)
from database.repositories.password_auth import (
    PasswordAuthValidationError as PostgresPasswordValidationError,
)
from database.repositories.password_reset import (
    InvalidPasswordResetCodeError as PostgresInvalidPasswordResetCodeError,
    PasswordResetError as PostgresPasswordResetError,
    PasswordResetRateLimitedError as PostgresPasswordResetRateLimitedError,
)
from tools.auth.postgres_password_recovery import (
    create_postgres_password_recovery_service,
)
from tools.auth.postgres_email_registration import (
    create_postgres_email_registration_service,
)
register_session_routes(
    require_auth_session=_require_auth_session,
    password_auth_service_factory=create_password_auth_service,
    auth_session_service_factory=create_auth_session_service,
    session_cookie_name=SESSION_COOKIE_NAME,
)
register_provider_management_routes(
    require_auth_session=_require_auth_session,
    password_auth_service_factory=create_password_auth_service,
    last_login_method_error=LastLoginMethodError,
    account_not_found_error=AccountNotFoundError,
    password_auth_error=PasswordAuthError,
)
register_password_entry_routes(
    password_auth_service_factory=create_password_auth_service,
    issue_auth_session=_issue_auth_session,
    client_host=_client_host,
    username_validation_error=UsernameValidationError,
    password_validation_error=PasswordValidationError,
    username_already_exists_error=UsernameAlreadyExistsError,
    login_rate_limited_error=LoginRateLimitedError,
    invalid_credentials_error=InvalidCredentialsError,
    password_auth_error=PasswordAuthError,
)
register_password_management_routes(
    require_auth_session=_require_auth_session,
    password_auth_service_factory=create_password_auth_service,
    password_reset_service_factory=create_password_reset_service,
    auth_session_service_factory=create_auth_session_service,
    issue_auth_session=_issue_auth_session,
    client_host=_client_host,
    password_validation_error=PasswordValidationError,
    password_already_set_error=PasswordAlreadySetError,
    account_not_found_error=AccountNotFoundError,
    invalid_credentials_error=InvalidCredentialsError,
    login_rate_limited_error=LoginRateLimitedError,
    password_not_set_error=PasswordNotSetError,
    password_auth_error=PasswordAuthError,
    password_reset_not_configured_error=PasswordResetNotConfiguredError,
    verified_email_required_error=VerifiedEmailRequiredError,
    password_reset_rate_limited_error=PasswordResetRateLimitedError,
    invalid_password_reset_code_error=InvalidPasswordResetCodeError,
    password_reset_error=PasswordResetError,
)
register_username_routes(
    require_auth_session=_require_auth_session,
    password_auth_service_factory=create_password_auth_service,
    username_validation_error=UsernameValidationError,
    username_already_exists_error=UsernameAlreadyExistsError,
    username_change_cooldown_error=UsernameChangeCooldownError,
    account_not_found_error=AccountNotFoundError,
    password_auth_error=PasswordAuthError,
)
register_onboarding_status_route(
    password_auth_service_factory=create_password_auth_service,
    verify_onboarding_token=verify_onboarding_token,
    onboarding_token_error=OnboardingTokenError,
    onboarding_cookie_name=ONBOARDING_COOKIE_NAME,
    username_validation_error=UsernameValidationError,
    username_already_exists_error=UsernameAlreadyExistsError,
    username_already_set_error=UsernameAlreadySetError,
    account_not_found_error=AccountNotFoundError,
    password_auth_error=PasswordAuthError,
)
app.include_router(auth_router)
register_password_recovery_routes(
    recovery_service_factory=create_postgres_password_recovery_service,
    issue_auth_session=_issue_auth_session,
    invalid_code_error=PostgresInvalidPasswordResetCodeError,
    rate_limited_error=PostgresPasswordResetRateLimitedError,
    password_validation_error=PostgresPasswordValidationError,
    recovery_error=PostgresPasswordResetError,
)
app.include_router(password_recovery_router)
register_email_registration_routes(
    service_factory=create_postgres_email_registration_service,
    issue_auth_session=_issue_auth_session,
)
app.include_router(email_registration_router)

# =========================================================
# LINE Login
# =========================================================

@app.get("/api/auth/line/login")
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
            _require_auth_session(
                request
            )

        state = (
            create_line_state()
        )

        authorization_url = (
            build_line_authorization_url(
                state=state,
                callback_url=_oauth_callback_url(
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


# =========================================================
# LINE Login Callback
# =========================================================

@app.get("/api/auth/line/callback")
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
                callback_url=_oauth_callback_url(
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
                    _require_auth_session(
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
                _frontend_profile_url(
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
                _oauth_success_url(
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
            _issue_auth_session(
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
            url=_frontend_profile_url(
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

# =========================================================
# Google Login
# =========================================================

@app.get("/api/auth/google/login")
def google_login(request: Request):
    """
    開始 Google OAuth Login。
    """

    try:
        link_mode = request.query_params.get("mode") == "link"
        if link_mode:
            _require_auth_session(request)

        state = create_google_state()

        authorization_url = (
            build_google_authorization_url(
                state=state,
                callback_url=_oauth_callback_url(request, "google"),
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


# =========================================================
# Google Login Callback
# =========================================================

@app.get("/api/auth/google/callback")
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
            callback_url=_oauth_callback_url(request, "google"),
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
            session_account = _require_auth_session(request)
            account = create_password_auth_service().link_oauth_identity(
                user_id=session_account["user_id"],
                provider="google",
                provider_subject=google_user_id,
                display_name=display_name,
                email=email,
                email_verified=email_verified,
            )
            frontend_url = _frontend_profile_url("google", request=request)
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
            frontend_url = _oauth_success_url(request, "google")

        response = RedirectResponse(
            url=frontend_url,
            status_code=302,
        )

        if not link_mode:
            _issue_auth_session(
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
            url=_frontend_profile_url("google", request=request, error=str(error)),
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

@app.get("/api/conversations")
def list_conversations(
    request: Request,
    user_id: str = "anonymous",
):
    user_id = _chat_user_id(request, user_id)
    if (
        not user_id
        or user_id == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少登入 user_id",
        )

    try:
        agent = get_agent(
            user_id=user_id,
            conversation_id="default",
        )

        conversations = (
            agent.memory
            .list_conversations()
        )

        return {
            "ok": True,
            "user_id": user_id,
            "conversations":
                conversations,
        }

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "讀取對話清單失敗："
                f"{error}"
            ),
        )


@app.post("/api/conversations")
async def create_conversation(
    payload:
        ConversationCreateRequest,
    request: Request,
):
    user_id = _chat_user_id(
        request,
        payload.user_id,
    )

    if (
        not user_id
        or user_id == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少登入 user_id",
        )

    conversation_id = (
        uuid4().hex
    )

    # 只配置草稿 ID，不建立 GetZep Thread 或資料庫紀錄。
    return {
        "ok": True,
        "conversation": {
            "conversation_id": conversation_id,
            "title": "新對話",
        },
    }


class ConversationRenameRequest(BaseModel):
    user_id: str = "anonymous"
    title: str


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    payload: ConversationRenameRequest,
    request: Request,
):
    user_id = _chat_user_id(request, payload.user_id)
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=400, detail="缺少登入 user_id")

    try:
        agent = get_agent(
            user_id=user_id,
            conversation_id=normalize_conversation_id(conversation_id),
        )
        conversation = agent.memory.rename_conversation(
            conversation_id,
            payload.title,
        )
        return {"ok": True, "conversation": conversation}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except KeyError:
        raise HTTPException(status_code=404, detail="找不到指定的對話")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail="重新命名失敗") from error


@app.delete(
    "/api/conversations/{conversation_id}"
)
def delete_conversation(
    conversation_id: str,
    request: Request,
    user_id: str = "anonymous",
):
    """
    刪除登入使用者的指定對話。
    """

    normalized_user_id = _chat_user_id(
        request,
        user_id,
    )

    if (
        not normalized_user_id
        or normalized_user_id
        == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少登入 user_id",
        )

    normalized_conversation_id = (
        normalize_conversation_id(
            conversation_id
        )
    )

    session_key = (
        normalized_user_id,
        normalized_conversation_id,
    )

    try:
        agent = get_agent(
            user_id=
                normalized_user_id,

            conversation_id=
                normalized_conversation_id,
        )

        deleted = (
            agent.memory
            .delete_conversation(
                normalized_conversation_id
            )
        )

        _sessions.pop(
            session_key,
            None,
        )

        # 刪除採冪等設計：舊空白索引或已刪除資料都視為成功。

        if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
            from database.postgres.repositories.conversation import (
                postgres_conversation_repository,
            )

            postgres_conversation_repository.delete(
                user_id=normalized_user_id,
                conversation_id=normalized_conversation_id,
            )

        return {
            "ok": True,

            "user_id":
                normalized_user_id,

            "conversation_id":
                normalized_conversation_id,

            "message":
                "對話已刪除",
        }

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "刪除 GetZep 對話失敗："
                f"{error}"
            ),
        )


@app.get("/api/chat/history")
def chat_history(
    request: Request,
    user_id: str = "anonymous",
    conversation_id: str = "default",
    lastn: int = 100,
):
    user_id = _chat_user_id(request, user_id)
    if (
        not user_id
        or user_id == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少登入 user_id",
        )

    try:
        agent = get_agent(
            user_id=user_id,
            conversation_id=
                conversation_id,
        )

        messages = (
            agent.memory
            .get_history(
                lastn=lastn
            )
        )

        return {
            "ok": True,
            "user_id":
                user_id,

            "conversation_id":
                conversation_id,

            "messages":
                messages,
            "conversation_limit": conversation_limit_status(
                messages,
                policy=_CONVERSATION_LIMIT_POLICY,
            ),
        }

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "讀取 GetZep 對話失敗："
                f"{error}"
            ),
        )


async def _reserve_chat_ticket():
    try:
        return await _CHAT_ADMISSION.reserve()
    except AdmissionFull as error:
        raise HTTPException(
            status_code=503,
            detail="系統目前忙碌，請稍後再試。",
            headers={"Retry-After": "5"},
        ) from error


async def _wait_for_chat_ticket(ticket) -> float:
    started_at = time.perf_counter()
    try:
        queue_seconds = await ticket.wait(_CHAT_QUEUE_TIMEOUT_SECONDS)
    except AdmissionTimeout as error:
        observe_pipeline_stage(
            "chat",
            "queue_wait",
            started_at,
            status="timeout",
        )
        await ticket.release()
        raise HTTPException(
            status_code=503,
            detail="系統目前請求較多，排隊逾時，請稍後再試。",
            headers={"Retry-After": "5"},
        ) from error
    observe_pipeline_stage("chat", "queue_wait", started_at)
    return queue_seconds


@app.post("/api/v1/chat")
async def chat(
    req: ChatRequest,
    request: Request,
):
    user_id = _chat_user_id(request, req.uid)
    guard_ticket = await _begin_chat_security(request, req, user_id)
    ticket = None
    try:
        agent = get_agent_with_profile(
            user_id=user_id,
            conversation_id=req.cid,
        )
        history_for_limit = await _enforce_conversation_limit(
            agent=agent,
            request=request,
            user_id=user_id,
            incoming_message=req.message,
        )
        if not history_for_limit:
            await _guard_conversation_creation(request, user_id)
        ticket = await _reserve_chat_ticket()
        await _wait_for_chat_ticket(ticket)
        result = await run_in_threadpool(
            agent.process,
            req.message,
            conversation_flow=req.flow,
        )
        if isinstance(result, dict):
            result["conversation_limit"] = _completed_conversation_limit(
                history_for_limit, req.message, result
            )
        return result
    finally:
        if ticket is not None:
            await ticket.release()
        await _CHAT_GUARD.finish(guard_ticket)


@app.post("/api/chat/message")
async def chat_message(
    req: ChatRequest,
    request: Request,
):
    """
    前端統一聊天入口。

    user_id：
        登入帳號。

    conversation_id：
        目前獨立對話。
    """

    user_id = _chat_user_id(request, req.uid)
    conversation_id = normalize_conversation_id(req.cid)

    security_started = time.perf_counter()
    guard_ticket = await _begin_chat_security(request, req, user_id)
    observe_pipeline_stage(
        "chat",
        "security_guard",
        security_started,
    )

    ticket = None
    try:
        agent_session_started = time.perf_counter()
        agent = get_agent_with_profile(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        observe_pipeline_stage(
            "chat",
            "agent_session",
            agent_session_started,
        )

        conversation_limit_started = time.perf_counter()
        history_for_limit = await _enforce_conversation_limit(
            agent=agent,
            request=request,
            user_id=user_id,
            incoming_message=req.message,
        )
        if not history_for_limit:
            await _guard_conversation_creation(request, user_id)
        observe_pipeline_stage(
            "chat",
            "conversation_limit",
            conversation_limit_started,
        )

        admission_started = time.perf_counter()
        ticket = await _reserve_chat_ticket()
        observe_pipeline_stage(
            "chat",
            "admission_reserve",
            admission_started,
        )
        await _wait_for_chat_ticket(ticket)
        if req.flow == "skin_consult":
            result = await run_in_threadpool(
                agent.process,
                f"__knowledge_consult__:{req.message}",
                conversation_flow=req.flow,
            )
        else:
            result = await run_in_threadpool(
                agent.process,
                req.message,
                conversation_flow=req.flow,
            )
    finally:
        if ticket is not None:
            await ticket.release()
        await _CHAT_GUARD.finish(guard_ticket)

    if isinstance(result, dict):
        result["conversation_limit"] = _completed_conversation_limit(
            history_for_limit, req.message, result
        )
    if isinstance(
        result.get("reply"),
        str,
    ):
        result["reply"] = (
            result["reply"]
            .replace(
                "**",
                "",
            )
        )

    result["route"] = (
        req.flow
        if req.flow in {
            "product_search",
            "skin_record",
            "skin_consult",
        }
        else result.get("route")
        or _stage_to_route(
            result.get("stage", "")
        )
    )

    result["sessionId"] = (
        conversation_id
    )

    result["conversationId"] = (
        conversation_id
    )

    return result


def _encode_sse(event: str, payload: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


async def _release_stream_resources_when_done(producer, ticket, guard_ticket) -> None:
    """Keep limits held while a disconnected client's sync model call finishes."""
    try:
        await producer
    except BaseException:
        pass
    finally:
        await ticket.release()
        await _CHAT_GUARD.finish(guard_ticket)


@app.post("/api/chat/message/stream")
async def chat_message_stream(
    req: ChatRequest,
    request: Request,
):
    """SSE 聊天入口；知識諮詢會直接轉送模型文字增量。"""
    user_id = _chat_user_id(request, req.uid)
    conversation_id = normalize_conversation_id(req.cid)
    guard_ticket = await _begin_chat_security(request, req, user_id)
    try:
        agent = get_agent_with_profile(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        history_for_limit = await _enforce_conversation_limit(
            agent=agent,
            request=request,
            user_id=user_id,
            incoming_message=req.message,
        )
        if not history_for_limit:
            await _guard_conversation_creation(request, user_id)
        ticket = await _reserve_chat_ticket()
    except BaseException:
        await _CHAT_GUARD.finish(guard_ticket)
        raise

    async def event_stream():
        loop = asyncio.get_running_loop()
        producer = None
        events = asyncio.Queue()
        state = {"has_delta": False}

        def on_delta(delta: str) -> None:
            if not delta:
                return
            state["has_delta"] = True
            loop.call_soon_threadsafe(
                events.put_nowait,
                ("delta", {"text": delta}),
            )

        def run_agent() -> dict:
            process_input = (
                f"__knowledge_consult__:{req.message}"
                if req.flow == "skin_consult"
                else req.message
            )
            return agent.process(
                process_input,
                conversation_flow=req.flow,
                on_delta=on_delta if req.flow == "skin_consult" else None,
            )

        async def produce() -> None:
            try:
                result = await run_in_threadpool(run_agent)
                if isinstance(result, dict):
                    result["conversation_limit"] = _completed_conversation_limit(
                        history_for_limit, req.message, result
                    )
                    if isinstance(result.get("reply"), str):
                        result["reply"] = result["reply"].replace("**", "")
                    result["route"] = (
                        req.flow
                        if req.flow in {
                            "product_search",
                            "skin_record",
                            "skin_consult",
                        }
                        else result.get("route")
                        or _stage_to_route(result.get("stage", ""))
                    )
                    result["sessionId"] = conversation_id
                    result["conversationId"] = conversation_id
                    await events.put(("done", result))
            except Exception:
                await events.put(
                    (
                        "error",
                        {"message": "目前暫時無法完成回覆，請稍後再試。"},
                    )
                )

        try:
            try:
                if not ticket.acquired:
                    yield _encode_sse(
                        "status",
                        {"stage": "queued", "message": "目前使用人數較多，已進入排隊…"},
                    )
                queue_seconds = await _wait_for_chat_ticket(ticket)
            except HTTPException as error:
                yield _encode_sse(
                    "error",
                    {"message": str(error.detail), "retry_after": 5},
                )
                return

            yield _encode_sse(
                "status",
                {
                    "stage": "processing",
                    "message": "正在整理資料…",
                    "queue_ms": round(queue_seconds * 1000, 2),
                },
            )
            producer = asyncio.create_task(produce())
            while True:
                event, payload = await events.get()
                if event == "done" and not state["has_delta"]:
                    reply = str(payload.get("reply") or "")
                    if reply:
                        yield _encode_sse("delta", {"text": reply})
                yield _encode_sse(event, payload)
                if event in {"done", "error"}:
                    break
        finally:
            if producer is not None and not producer.done():
                # run_in_threadpool cannot reliably cancel an already-running
                # sync SDK call.  Keep both admission and account locks until
                # it really exits instead of allowing a cost-amplifying retry.
                asyncio.create_task(
                    _release_stream_resources_when_done(
                        producer,
                        ticket,
                        guard_ticket,
                    )
                )
            else:
                await ticket.release()
                await _CHAT_GUARD.finish(guard_ticket)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/v1/session/clear")
def clear_session(
    request: Request,
    user_id: str = "anonymous",
    conversation_id: str = "default",
):
    user_id = _chat_user_id(request, user_id)
    if (
        not user_id
        or user_id == "anonymous"
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少登入 user_id",
        )

    normalized_conversation_id = (
        normalize_conversation_id(
            conversation_id
        )
    )

    agent = get_agent(
        user_id=user_id,
        conversation_id=
            normalized_conversation_id,
    )

    try:
        agent.memory.clear_thread()

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "清除 GetZep 對話失敗："
                f"{error}"
            ),
        )

    agent.state.reset()

    return {
        "ok": True,
        "user_id":
            user_id,

        "conversation_id":
            normalized_conversation_id,

        "message":
            "已清除指定的 GetZep 對話",
    }


"""
BLINK AI API 第三段：skin-records CRUD + reports
用法：把本檔內容 append 到 api/api_server.py 尾端（或 import 進去），重啟 uvicorn。
對接：tools/agent/tracking/skin_record_tool.SkinRecordStore（Neo4j）
     tools/agent/tracking/skin_report_tool.build_report / render_report_text
"""
# ══════════ 第三段：skin-records CRUD + reports ══════════
from datetime import date as _Date
from fastapi import HTTPException

from tools.agent.tracking.skin_record_tool import SkinRecordStore
from tools.agent.tracking.skin_save_tool import save_skin_record
from tools.agent.tracking.skin_report_tool import build_report, render_report_text

_store = None

def _current_skin_user_id(request: Request, requested_user_id: str = "anonymous") -> str:
    if (
        os.getenv("ENABLE_TEST_MOCKS", "") == "1"
        and request.query_params.get("mock") == "5days"
    ):
        return "badd4a8a-d651-4786-afe3-9d02dce71b1c"
    return _chat_user_id(request, requested_user_id)


def get_store() -> SkinRecordStore:
    global _store
    if _store is None:
        if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
            from database.postgres.repositories.skin_record import (
                postgres_skin_record_store,
            )

            _store = postgres_skin_record_store
        else:
            _store = SkinRecordStore()   # 讀 .env 的 NEO4J_URI/USER/PASSWORD
    return _store


class SkinRecordCreate(BaseModel):
    user_id: str = "anonymous"
    issue: str                      # dryness / acne / redness ...
    zone: str                       # left_cheek / chin / forehead ...
    severity: int                   # 1~5
    note: str = ""
    products: list[str] = []
    duration_days: Optional[int] = None


class SkinRecordPatch(BaseModel):
    user_id: str = "anonymous"
    severity: Optional[int] = None
    note: Optional[str] = None
    issue: Optional[str] = None
    zone: Optional[str] = None


@app.post("/api/skin-records")
def create_skin_record(req: SkinRecordCreate, request: Request):
    """膚況紀錄表單直存（結構化路徑，不經 TAIDE）
    走 save_skin_record 以套用同學的標準化＋連勝天數計算"""
    if not (1 <= req.severity <= 5):
        raise HTTPException(400, "severity 必須是 1~5")
    draft = {
        "intent": "create",
        "issue": req.issue,
        "zone": req.zone,
        "severity": req.severity,
        "note": req.note,
        "products": req.products,
        "duration_days": req.duration_days,
    }
    try:
        result = save_skin_record(
            get_store(), _current_skin_user_id(request, req.user_id), draft
        )
        return {"ok": True, **(result or {})}
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Failed to save skin record")
        raise HTTPException(status_code=500, detail="存檔失敗") from None


@app.get("/api/skin-records")
def list_skin_records(request: Request, user_id: str = "anonymous",
                      date_from: Optional[str] = None,
                      date_to: Optional[str] = None):
    """歷史紀錄查詢（date_from/date_to 格式 YYYY-MM-DD，可省略）"""
    try:
        uid = _current_skin_user_id(request, user_id)
        records = get_store().query(uid, date_from=date_from, date_to=date_to)
        return {"ok": True, "count": len(records), "records": records}
    except HTTPException:
        raise
    except TypeError:
        # 同學的 query 若沒有 date_to 參數
        records = get_store().query(uid, date_from=date_from)
        return {"ok": True, "count": len(records), "records": records}
    except Exception:
        logging.getLogger(__name__).exception("Failed to list skin records")
        raise HTTPException(status_code=500, detail="查詢失敗") from None


@app.patch("/api/skin-records/{record_id}")
def patch_skin_record(record_id: str, req: SkinRecordPatch, request: Request):
    """修改紀錄（只更新有帶的欄位）"""
    fields = {k: v for k, v in req.model_dump().items()
              if k != "user_id" and v is not None}
    if not fields:
        raise HTTPException(400, "沒有要更新的欄位")
    if "severity" in fields and not (1 <= fields["severity"] <= 5):
        raise HTTPException(400, "severity 必須是 1~5")
    ok = get_store().update(
        _current_skin_user_id(request, req.user_id), record_id, fields
    )
    if not ok:
        raise HTTPException(404, "找不到這筆紀錄")
    return {"ok": True, "updated": list(fields.keys())}


@app.delete("/api/skin-records/{record_id}")
def delete_skin_record(record_id: str, request: Request, user_id: str = "anonymous"):
    ok = get_store().delete(_current_skin_user_id(request, user_id), record_id)
    if not ok:
        raise HTTPException(404, "找不到這筆紀錄，可能已被刪除")
    return {"ok": True}


@app.get("/api/skin-reports/current")
def current_skin_report(request: Request, user_id: str = "anonymous"):
    """最新膚況分析：先查連續天數資格，不足回進度非error（前端畫進度條）"""
    store = get_store()
    user_id = _current_skin_user_id(request, user_id)
    try:
        elig = store.check_eligibility(user_id, min_days=5)
    except Exception as e:
        raise HTTPException(500, f"資格查詢失敗：{e}")

    if not elig.get("eligible", False):
        return {
            "ok": True, "eligible": False,
            "streak_days": elig.get("consecutive_days", elig.get("streak_days", 0)),
            "min_days": 5,
            "message": "膚況分析表需要連續 5 天的紀錄才能產出，繼續加油！",
        }

    try:
        report = build_report(store, user_id)          # 簽名若不同見下方 fallback
    except TypeError:
        records = store.query(user_id)
        report = build_report(records)
    try:
        text = render_report_text(report)
    except Exception:
        text = str(report)

    return {"ok": True, "eligible": True,
            "report_text": text,
            "report": report.model_dump() if hasattr(report, "model_dump") else report}


# ══════════ 第三段：skin-records CRUD + reports ══════════
from fastapi import HTTPException

from tools.agent.tracking.skin_record_tool import SkinRecordStore
from tools.agent.tracking.skin_save_tool import save_skin_record
from tools.agent.tracking.skin_report_tool import build_report, render_report_text

_store = None

def get_store() -> SkinRecordStore:
    global _store
    if _store is None:
        if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
            from database.postgres.repositories.skin_record import (
                postgres_skin_record_store,
            )

            _store = postgres_skin_record_store
        else:
            _store = SkinRecordStore()
    return _store


class SkinRecordCreate(BaseModel):
    user_id: str = "anonymous"
    issue: str
    zone: str
    severity: int
    note: str = ""
    products: list[str] = []
    duration_days: Optional[int] = None


class SkinRecordPatch(BaseModel):
    user_id: str = "anonymous"
    severity: Optional[int] = None
    note: Optional[str] = None
    issue: Optional[str] = None
    zone: Optional[str] = None


@app.post("/api/skin-records")
def create_skin_record(req: SkinRecordCreate, request: Request):
    if not (1 <= req.severity <= 5):
        raise HTTPException(400, "severity 必須是 1~5")
    draft = {
        "intent": "create", "issue": req.issue, "zone": req.zone,
        "severity": req.severity, "note": req.note,
        "products": req.products, "duration_days": req.duration_days,
    }
    try:
        result = save_skin_record(
            get_store(), _current_skin_user_id(request, req.user_id), draft
        )
        return {"ok": True, **(result or {})}
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Failed to save skin record")
        raise HTTPException(status_code=500, detail="存檔失敗") from None


@app.get("/api/skin-records")
def list_skin_records(request: Request, user_id: str = "anonymous",
                      date_from: Optional[str] = None,
                      date_to: Optional[str] = None):
    try:
        uid = _current_skin_user_id(request, user_id)
        records = get_store().query(uid, date_from=date_from, date_to=date_to)
    except HTTPException:
        raise
    except TypeError:
        records = get_store().query(uid, date_from=date_from)
    except Exception:
        logging.getLogger(__name__).exception("Failed to list skin records")
        raise HTTPException(status_code=500, detail="查詢失敗") from None
    return {"ok": True, "count": len(records), "records": records}


@app.patch("/api/skin-records/{record_id}")
def patch_skin_record(record_id: str, req: SkinRecordPatch, request: Request):
    fields = {k: v for k, v in req.model_dump().items()
              if k != "user_id" and v is not None}
    if not fields:
        raise HTTPException(400, "沒有要更新的欄位")
    if "severity" in fields and not (1 <= fields["severity"] <= 5):
        raise HTTPException(400, "severity 必須是 1~5")
    ok = get_store().update(
        _current_skin_user_id(request, req.user_id), record_id, fields
    )
    if not ok:
        raise HTTPException(404, "找不到這筆紀錄")
    return {"ok": True, "updated": list(fields.keys())}


@app.delete("/api/skin-records/{record_id}")
def delete_skin_record(record_id: str, request: Request, user_id: str = "anonymous"):
    ok = get_store().delete(_current_skin_user_id(request, user_id), record_id)
    if not ok:
        raise HTTPException(404, "找不到這筆紀錄，可能已被刪除")
    return {"ok": True}


@app.get("/api/skin-reports/current")
def current_skin_report(request: Request, user_id: str = "anonymous"):
    store = get_store()
    user_id = _current_skin_user_id(request, user_id)
    try:
        elig = store.check_eligibility(user_id, min_days=5)
    except Exception as e:
        raise HTTPException(500, f"資格查詢失敗：{e}")

    if not elig.get("eligible", False):
        return {"ok": True, "eligible": False,
                "streak_days": elig.get("consecutive_days", elig.get("streak_days", 0)),
                "min_days": 5,
                "message": "膚況分析表需要連續 5 天的紀錄才能產出，繼續加油！"}

    try:
        report = build_report(store, user_id)
    except TypeError:
        report = build_report(store.query(user_id))
    try:
        text = render_report_text(report)
    except Exception:
        text = str(report)
    return {"ok": True, "eligible": True, "report_text": text,
            "report": report.model_dump() if hasattr(report, "model_dump") else report}


# ══════════ 前端相容層：skin-records 她的payload / profile / checkin ══════════
from datetime import datetime, timezone

_REGION_TO_ZONE = {"t_zone": "t_zone", "left_cheek": "left_cheek",
                   "right_cheek": "right_cheek", "chin": "chin"}
_CONDITION_TO_ISSUE = {
    "dryness": "dryness", "oiliness": "oiliness", "tightness": "dryness",
    "redness_feeling": "redness", "roughness": "dryness",
    "sticky_feeling": "oiliness", "stable": "stable",
}

class FrontendSkinRecord(BaseModel):
    recordDate: Optional[str] = None
    region: str
    conditions: list[str]
    severity: int
    notes: str = ""
    user_id: Optional[str] = None
    session_id: Optional[str] = None


@app.post("/api/skin-records/frontend")
def create_skin_record_frontend(req: FrontendSkinRecord, request: Request):
    """前端 SkinRecordPayload 直入口（欄位映射後走同一 save）"""
    uid = _current_skin_user_id(
        request, req.user_id or req.session_id or "anonymous"
    )
    issue = _CONDITION_TO_ISSUE.get(req.conditions[0], req.conditions[0]) \
            if req.conditions else "stable"
    extra = "、".join(req.conditions[1:]) if len(req.conditions) > 1 else ""
    note = req.notes + (f"（另有：{extra}）" if extra else "")
    draft = {"intent": "create", "issue": issue,
             "zone": _REGION_TO_ZONE.get(req.region, req.region),
             "severity": req.severity, "note": note, "products": []}
    try:
        result = save_skin_record(get_store(), uid, draft)
        return {"ok": True, **(result or {})}
    except Exception as e:
        raise HTTPException(500, f"存檔失敗：{e}")


class ProfileForm(BaseModel):
    name: str = ""
    gender: str = ""
    birthday: str = ""
    email: str = ""
    lineBound: bool = False
    skinType: str = ""
    allergens: list[str] = []
    otherAllergen: str = ""
    customerType: str = ""
    user_id: Optional[str] = None
    session_id: Optional[str] = None

_SKINTYPE_MAP = {"油性肌": "oily", "乾性肌": "dry", "中性肌": "normal",
                 "混合肌": "combination", "敏感肌": "sensitive"}




# ══════════ 每日簽到 / 集點 / 功能解鎖 API ══════════
from zoneinfo import ZoneInfo


CHECKIN_TOTAL_DAYS = 14
CHECKIN_POINTS_PER_DAY = 10

BEAUTY_UNLOCK_DAY = 5
DOCTOR_UNLOCK_DAY = 7

CHECKIN_TIMEZONE_NAME = "Asia/Taipei"
CHECKIN_TIMEZONE = ZoneInfo(CHECKIN_TIMEZONE_NAME)


def _resolve_checkin_uid(
    user_id: str = "anonymous",
    session_id: Optional[str] = None,
    request: Optional[Request] = None,
) -> str:
    """Derive an authenticated owner or accept only an isolated guest id."""
    if request is None:
        raise InvalidSessionError("請先登入")
    proposed_user_id = user_id if user_id and user_id != "anonymous" else session_id
    resolved_user_id = _chat_user_id(request, proposed_user_id or "anonymous")

    # PostgreSQL check-ins belong to registered users and use UUID keys.
    # Reject stale sessions and guest identifiers at the API boundary.
    if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
        try:
            return str(UUID(resolved_user_id))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=401,
                detail="登入狀態無效，請重新登入",
            ) from None

    return resolved_user_id


def _get_checkin_collection():
    """Return the PostgreSQL daily check-in repository."""
    from database.postgres.repositories.checkin import (
        postgres_daily_checkin_collection,
    )

    return postgres_daily_checkin_collection

def _get_checkin_time():
    """
    回傳：
    1. UTC 時間：方便資料庫統一排序
    2. 台灣時間：每日簽到日期判斷

    非常重要：
    每日是否已簽到要使用 Asia/Taipei，
    不能直接拿 UTC 日期。
    """
    now_utc = datetime.now(timezone.utc)

    now_taipei = now_utc.astimezone(
        CHECKIN_TIMEZONE
    )

    return now_utc, now_taipei


def _get_user_checkin_records(
    user_id: str,
):
    """
    取得某位使用者所有簽到紀錄。

    舊資料只有：
    - user_id
    - date
    - created_at

    新資料則會再有：
    - checked_in_at
    - timezone
    - points_earned
    - cycle_number
    - day_number

    因此這裡同時相容舊資料。
    """
    col = _get_checkin_collection()

    records = list(
        col.find(
            {
                "user_id": user_id,
            },
            {
                "_id": 0,
            },
        ).sort(
            [
                ("date", 1),
                ("created_at", 1),
            ]
        )
    )

    return records


def _build_checkin_status(
    user_id: str,
):
    """
    根據 MongoDB 所有簽到紀錄，
    建立前端每日簽到頁需要的完整狀態。

    規則：
    - 14 天一輪
    - 一個台灣日期只能簽一次
    - 每次 +10P
    - Day 14 完成當天維持 14/14
    - 下一個日期才進下一輪
    - 總點數不重置
    - Day 5 美妝小知識永久解鎖
    - Day 7 皮膚科建議永久解鎖
    """
    records = _get_user_checkin_records(
        user_id
    )

    _, now_taipei = _get_checkin_time()

    today = now_taipei.strftime(
        "%Y-%m-%d"
    )

    total_count = len(records)

    total_points = sum(
        int(
            record.get(
                "points_earned",
                CHECKIN_POINTS_PER_DAY,
            )
        )
        for record in records
    )

    today_checked_in = any(
        str(
            record.get(
                "date",
                "",
            )
        )
        == today
        for record in records
    )

    full_cycles = (
        total_count
        // CHECKIN_TOTAL_DAYS
    )

    remainder = (
        total_count
        % CHECKIN_TOTAL_DAYS
    )

    current_cycle = 1
    current_cycle_records = []
    cycle_completed = False

    # -----------------------------------------------------
    # 完全沒有任何簽到
    # -----------------------------------------------------

    if total_count == 0:
        current_cycle = 1

        current_cycle_records = []

        cycle_completed = False

    # -----------------------------------------------------
    # 剛好是 14、28、42... 筆
    # -----------------------------------------------------

    elif remainder == 0:

        last_record = records[-1]

        last_date = str(
            last_record.get(
                "date",
                "",
            )
        )

        if last_date >= today:
            current_cycle = max(
                full_cycles,
                1,
            )

            current_cycle_records = (
                records[
                    -CHECKIN_TOTAL_DAYS:
                ]
            )

            cycle_completed = True

        else:
            current_cycle = (
                full_cycles + 1
            )

            current_cycle_records = []

            cycle_completed = False

    # -----------------------------------------------------
    # 本輪還沒做到 Day 14
    # -----------------------------------------------------

    else:
        current_cycle = (
            full_cycles + 1
        )

        current_cycle_records = (
            records[-remainder:]
        )

        cycle_completed = False

    completed_count = len(
        current_cycle_records
    )

    completed_days = list(
        range(
            1,
            completed_count + 1,
        )
    )

    if completed_count >= CHECKIN_TOTAL_DAYS:
        next_day = None

    else:
        next_day = (
            completed_count + 1
        )

    beauty_unlocked = (
        total_count
        >= BEAUTY_UNLOCK_DAY
    )

    doctor_unlocked = (
        total_count
        >= DOCTOR_UNLOCK_DAY
    )

    beauty_remaining = (
        0
        if beauty_unlocked
        else max(
            BEAUTY_UNLOCK_DAY
            - total_count,
            0,
        )
    )

    doctor_remaining = (
        0
        if doctor_unlocked
        else max(
            DOCTOR_UNLOCK_DAY
            - total_count,
            0,
        )
    )

    # -----------------------------------------------------
    # 回傳當輪每一天的日期與時間
    # -----------------------------------------------------

    current_cycle_checkins = []

    for index, record in enumerate(
        current_cycle_records,
        start=1,
    ):
        checked_in_at = (
            record.get(
                "checked_in_at"
            )
            or record.get(
                "created_at"
            )
        )

        current_cycle_checkins.append(
            {
                "day_number": index,

                "date":
                    record.get(
                        "date"
                    ),

                "checked_in_at":
                    checked_in_at,

                "points_earned":
                    int(
                        record.get(
                            "points_earned",
                            CHECKIN_POINTS_PER_DAY,
                        )
                    ),
            }
        )

    last_checkin_at = None

    if records:
        last_record = records[-1]

        last_checkin_at = (
            last_record.get(
                "checked_in_at"
            )
            or last_record.get(
                "created_at"
            )
        )

    can_check_in = (
        not today_checked_in
        and not cycle_completed
    )

    return {
        "ok": True,

        "user_id":
            user_id,

        "timezone":
            CHECKIN_TIMEZONE_NAME,

        "today":
            today,

        "points":
            total_points,

        "total_checkins":
            total_count,

        "current_cycle":
            current_cycle,

        "completed_days":
            completed_days,

        "completed_count":
            completed_count,

        "current_cycle_checkins":
            current_cycle_checkins,

        "today_checked_in":
            today_checked_in,

        "can_check_in":
            can_check_in,

        "cycle_completed":
            cycle_completed,

        "next_day":
            next_day,

        "beauty_unlocked":
            beauty_unlocked,

        "doctor_unlocked":
            doctor_unlocked,

        "beauty_remaining":
            beauty_remaining,

        "doctor_remaining":
            doctor_remaining,

        "last_checkin_at":
            last_checkin_at,
    }


# =========================================================
# GET：取得目前簽到頁狀態
# =========================================================

@app.get("/api/checkin/status")
def get_daily_checkin_status(
    request: Request,
    user_id: str = "anonymous",
    session_id: Optional[str] = None,
):
    """
    前端一進入 /daily-checkin 時呼叫。

    回傳：
    - 點數
    - 現在第幾輪
    - 本輪完成幾天
    - 今日是否已簽
    - Day 5 / Day 7 是否解鎖
    """
    uid = _resolve_checkin_uid(
        user_id,
        session_id,
        request,
    )

    try:
        return _build_checkin_status(
            uid
        )

    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Failed to get check-in status")
        raise HTTPException(
            status_code=500,
            detail="取得簽到狀態失敗",
        ) from None


# =========================================================
# POST：今日簽到
# =========================================================

@app.post("/api/checkin")
def daily_checkin(
    request: Request,
    user_id: str = "anonymous",
    session_id: Optional[str] = None,
):
    """
    每日簽到。

    後端負責：
    - 判斷台灣今天日期
    - 防止同日重複簽到
    - 計算 Day 1 ~ Day 14
    - +10P
    - 記錄真正簽到時間
    - 判斷功能解鎖
    """
    uid = _resolve_checkin_uid(
        user_id,
        session_id,
        request,
    )

    try:
        before_status = (
            _build_checkin_status(
                uid
            )
        )

        # -------------------------------------------------
        # 今天已經簽過
        # -------------------------------------------------

        if before_status[
            "today_checked_in"
        ]:
            return {
                "ok": True,

                "already": True,

                "message":
                    "今天已經簽到過囉，明天再回來累積下一天 ♡",

                "checkin": None,

                "status":
                    before_status,
            }

        # -------------------------------------------------
        # Day 14 當天已完成
        # 理論上 today_checked_in 已經會擋住，
        # 這裡多一層保護。
        # -------------------------------------------------

        if not before_status[
            "can_check_in"
        ]:
            return {
                "ok": True,

                "already": True,

                "message":
                    f"第 {before_status['current_cycle']} 輪已完成 ♡ 明天會自動開始下一輪。",

                "checkin": None,

                "status":
                    before_status,
            }

        next_day = (
            before_status[
                "next_day"
            ]
            or 1
        )

        cycle_number = (
            before_status[
                "current_cycle"
            ]
        )

        now_utc, now_taipei = (
            _get_checkin_time()
        )

        today = (
            now_taipei.strftime(
                "%Y-%m-%d"
            )
        )

        checkin_document = {
            "user_id":
                uid,

            # 台灣日期
            "date":
                today,

            # UTC，保留統一的系統時間
            "created_at":
                now_utc.isoformat(),

            # 使用者看到的台灣簽到時間
            "checked_in_at":
                now_taipei.isoformat(),

            "timezone":
                CHECKIN_TIMEZONE_NAME,

            "points_earned":
                CHECKIN_POINTS_PER_DAY,

            "cycle_number":
                cycle_number,

            "day_number":
                next_day,
        }

        col = _get_checkin_collection()

        # 再檢查一次，
        # 避免同一時間重複按鈕造成兩筆。
        existing = col.find_one(
            {
                "user_id":
                    uid,

                "date":
                    today,
            }
        )

        if existing:
            status = (
                _build_checkin_status(
                    uid
                )
            )

            return {
                "ok": True,

                "already": True,

                "message":
                    "今天已經簽到過囉，明天再回來累積下一天 ♡",

                "checkin": None,

                "status":
                    status,
            }

        col.insert_one(
            checkin_document
        )

        after_status = (
            _build_checkin_status(
                uid
            )
        )

        beauty_just_unlocked = (
            not before_status[
                "beauty_unlocked"
            ]
            and after_status[
                "beauty_unlocked"
            ]
        )

        doctor_just_unlocked = (
            not before_status[
                "doctor_unlocked"
            ]
            and after_status[
                "doctor_unlocked"
            ]
        )

        if next_day == CHECKIN_TOTAL_DAYS:
            message = (
                f"✦ 第 {next_day} 天簽到成功，"
                f"獲得 {CHECKIN_POINTS_PER_DAY}P ♡"
            )

        elif doctor_just_unlocked:
            message = (
                f"✦ 第 {next_day} 天簽到成功，"
                f"獲得 {CHECKIN_POINTS_PER_DAY}P，"
                "皮膚科建議已解鎖！"
            )

        elif beauty_just_unlocked:
            message = (
                f"✦ 第 {next_day} 天簽到成功，"
                f"獲得 {CHECKIN_POINTS_PER_DAY}P，"
                "每日美妝小知識已解鎖！"
            )

        else:
            message = (
                f"✦ 第 {next_day} 天簽到成功，"
                f"獲得 {CHECKIN_POINTS_PER_DAY}P ♡"
            )

        checkin_result = {
            key: value
            for key, value
            in checkin_document.items()
            if key != "_id"
        }

        return {
            "ok": True,

            "already":
                False,

            "message":
                message,

            "checkin":
                checkin_result,

            "status":
                after_status,
        }

    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Failed to create daily check-in")
        raise HTTPException(
            status_code=500,
            detail="簽到失敗",
        ) from None


# =========================================================
# GET：查看所有簽到歷史
# =========================================================

@app.get("/api/checkin/history")
def get_daily_checkin_history(
    request: Request,
    user_id: str = "anonymous",
    session_id: Optional[str] = None,
):
    """
    查詢使用者所有簽到時間。

    目前頁面不一定會顯示，
    但後端已完整保留紀錄，
    之後做歷史頁可以直接使用。
    """
    uid = _resolve_checkin_uid(
        user_id,
        session_id,
        request,
    )

    try:
        records = (
            _get_user_checkin_records(
                uid
            )
        )

        history = []

        for index, record in enumerate(
            records
        ):
            cycle_number = (
                index
                // CHECKIN_TOTAL_DAYS
            ) + 1

            day_number = (
                index
                % CHECKIN_TOTAL_DAYS
            ) + 1

            history.append(
                {
                    "cycle_number":
                        cycle_number,

                    "day_number":
                        day_number,

                    "date":
                        record.get(
                            "date"
                        ),

                    "checked_in_at":
                        (
                            record.get(
                                "checked_in_at"
                            )
                            or record.get(
                                "created_at"
                            )
                        ),

                    "timezone":
                        record.get(
                            "timezone",
                            CHECKIN_TIMEZONE_NAME,
                        ),

                    "points_earned":
                        int(
                            record.get(
                                "points_earned",
                                CHECKIN_POINTS_PER_DAY,
                            )
                        ),
                }
            )

        history.reverse()

        return {
            "ok": True,

            "user_id":
                uid,

            "count":
                len(history),

            "history":
                history,
        }

    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Failed to get check-in history")
        raise HTTPException(
            status_code=500,
            detail="取得簽到歷史失敗",
        ) from None
from api.profile_api import (
    create_profile_router,
 )

app.include_router(
    create_profile_router(get_agent, _require_auth_session)
)

app.include_router(
    create_quiz_router(
        require_auth_session=_require_auth_session,
        get_questions=get_quiz_questions,
        record_result=record_quiz_result,
        get_latest_result=get_latest_quiz_result,
    )
)
