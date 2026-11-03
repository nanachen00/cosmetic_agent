"""BLINK AI compatibility ASGI composition root."""
import os
import sys
import time
import logging
import hmac
import ipaddress
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')
import hashlib
import secrets
from threading import Lock
from uuid import UUID
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional

from agents.root.orchestrator import RootAgentOrchestrator
from app.security.request_guard import (
    FixedWindowLimiter,
    RateLimiterUnavailable,
    RateLimitRule,
    RedisFixedWindowLimiter,
)
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
from core.admission import (
    AdmissionFull,
    AdmissionTimeout,
    RedisChatAdmissionController,
    create_chat_admission_controller,
)
from core.observability import install_observability
from core.tracing import configure_tracing
from core.observability import (
    observe_chat_overload,
    observe_pipeline_stage,
    set_chat_admission_state,
)

from tools.auth.password_login import (
    AccountNotFoundError,
    InvalidCredentialsError,
    LastLoginMethodError,
    LoginRateLimitedError,
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
    OnboardingTokenError,
    build_onboarding_url,
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

from core.usage_tracking import install_usage_tracking
install_usage_tracking()

app = FastAPI(title="BLINK AI MAS API", version="0.1")
configure_tracing(app)
_CHAT_SECURITY_LOGGER = logging.getLogger("blink.chat_security")
_CHAT_GUARD = ChatGuard()
_CHAT_SECURITY_LOG_LIMITER = FixedWindowLimiter(max_buckets=10_000)
_CHAT_SECURITY_LOG_HMAC_SECRET = (
    os.getenv("CHAT_SECURITY_LOG_HMAC_SECRET", "").strip().encode("utf-8")
    or os.getenv("CHAT_GUEST_SESSION_SECRET", "").strip().encode("utf-8")
    or secrets.token_bytes(32)
)
_GUEST_COOKIE_NAME = (
    os.getenv("CHAT_GUEST_COOKIE_NAME", "blink_guest_session").strip()
    or "blink_guest_session"
)
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
_DEFAULT_FRONTEND_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:13001",
    "http://127.0.0.1:13001",
    "https://blinkai.tw",
    "https://www.blinkai.tw",
}


def _origins_from_env(name: str) -> set[str]:
    configured = os.getenv(name, "").strip()
    if not configured:
        return set(_DEFAULT_FRONTEND_ORIGINS)
    origins = {
        origin.strip().rstrip("/")
        for origin in configured.split(",")
        if origin.strip()
    }
    if "*" in origins:
        raise RuntimeError(f"{name} cannot contain wildcard when credentials are enabled")
    if not origins:
        raise RuntimeError(f"{name} must contain at least one explicit origin")
    return origins


SESSION_TRUSTED_ORIGINS = _origins_from_env("SESSION_TRUSTED_ORIGINS")
CORS_ALLOW_ORIGINS = _origins_from_env("CORS_ALLOW_ORIGINS")
@app.middleware("http")
async def reject_cross_site_session_requests(request: Request, call_next):
    return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked"}) if (request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get(SESSION_COOKIE_NAME) and (request.headers.get("sec-fetch-site", "").lower() == "cross-site" or ((origin := request.headers.get("origin", "").rstrip("/")) and origin not in SESSION_TRUSTED_ORIGINS))) else await call_next(request)


_API_RATE_LIMITER = RedisFixedWindowLimiter()
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
    guest_token = request.cookies.get("blink_guest_session", "").strip()
    identity_token = session_token or guest_token
    if identity_token:
        digest = hashlib.sha256(identity_token.encode("utf-8")).hexdigest()[:24]
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

    rule = _API_RATE_LIMITS.get((request.method, request.url.path))

    if rule is not None:
        identity = _request_limit_identity(request)
        bucket = "ai-chat" if request.url.path in {"/api/v1/chat", "/api/chat/message"} else request.url.path
        try:
            retry_after = await _API_RATE_LIMITER.check(
                f"{request.method}:{bucket}:{identity}",
                rule,
            )
        except RateLimiterUnavailable:
            return JSONResponse(
                status_code=503,
                content={"detail": "Rate limiter unavailable"},
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
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
    allow_origins=sorted(CORS_ALLOW_ORIGINS),
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
_CHAT_ADMISSION = create_chat_admission_controller(
    max_running=int(os.getenv("CHAT_MAX_RUNNING", "8")),
    max_queued=int(os.getenv("CHAT_MAX_QUEUED", "32")),
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


def _redis_async_state_ready() -> bool:
    admission_required = os.getenv("CHAT_ADMISSION_REQUIRE_REDIS", "0") == "1"
    job_store_required = os.getenv("CHAT_JOB_STORE_REQUIRE_REDIS", "0") == "1"
    if not admission_required and not job_store_required:
        return True

    from redis.asyncio import Redis as AsyncRedis

    del AsyncRedis
    if admission_required and not isinstance(
        _CHAT_ADMISSION, RedisChatAdmissionController
    ):
        return False
    if job_store_required:
        from app.services.chat_job_store import (
            RedisChatJobStore,
            chat_job_store,
        )

        if not isinstance(chat_job_store, RedisChatJobStore):
            return False
    return True


def _tracing_ready() -> bool:
    enabled = os.getenv("OTEL_TRACING_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return not enabled or bool(
        getattr(app.state, "blink_tracing_enabled", False)
    )


def _release_ready() -> bool:
    required = os.getenv("APP_RELEASE_REQUIRED", "0") == "1"
    return not required or bool(os.getenv("APP_RELEASE", "").strip())


def _internal_http_ready(url: str) -> bool:
    from urllib.request import urlopen

    with urlopen(url, timeout=1.5) as response:
        return response.status == 200


_READINESS_CHECKS = {
    "postgres": _postgres_ready,
    "redis": _redis_ready,
    "redis_async_state": _redis_async_state_ready,
    "otel": _tracing_ready,
    "release": _release_ready,
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


from app.schemas.chat import ChatRequest, normalize_conversation_id


def get_agent(
    user_id: str,
    conversation_id: str = "default",
) -> RootAgentOrchestrator:
    """Return a cached agent without serializing slow constructors globally."""
    conversation_id = normalize_conversation_id(conversation_id)
    session_key = (user_id, conversation_id)

    with _sessions_lock:
        agent = _sessions.pop(session_key, None)
        if agent is not None:
            _sessions[session_key] = agent
            return agent

    # RootAgentOrchestrator may read profile/memory from remote services.
    # Never hold the process-wide cache lock during those network calls.
    candidate = RootAgentOrchestrator(
        user_id=user_id,
        conversation_id=conversation_id,
    )

    with _sessions_lock:
        agent = _sessions.pop(session_key, None) or candidate
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


from app.api.routes.guest_session import create_guest_session_router

app.include_router(
    create_guest_session_router(
        signer_provider=_guest_signer,
        cookie_name=_GUEST_COOKIE_NAME,
        cookie_ttl_seconds=_GUEST_COOKIE_TTL_SECONDS,
    )
)


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
            "http://localhost:8010",
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

# BEGIN knowledge-explorer-spike: disabled unless KNOWLEDGE_GRAPH_ENABLED=1.
from app.api.routes.knowledge_explorer import create_knowledge_explorer_router
app.include_router(create_knowledge_explorer_router())
# END knowledge-explorer-spike

from app.api.routes.welcome import create_welcome_router
from app.services.welcome_service import WelcomeService

app.include_router(
    create_welcome_router(
        owner_resolver=_chat_user_id,
        welcome_service=WelcomeService(get_agent),
    )
)

from app.api.routes.conversations import create_conversation_router
from app.services.conversation_service import ConversationService
from app.services.conversation_repository import ConversationRepository

app.include_router(
    create_conversation_router(
        owner_resolver=_chat_user_id,
        service=ConversationService(
            agent_provider=lambda **kwargs: get_agent(**kwargs),
            normalize_conversation_id=normalize_conversation_id,
            sessions=_sessions,
            conversation_limit_status=conversation_limit_status,
            conversation_limit_policy=_CONVERSATION_LIMIT_POLICY,
            repository=ConversationRepository(),
        ),
    )
)


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

from app.api.routes.oauth import OAuthRouteDependencies, create_oauth_router


app.include_router(
    create_oauth_router(
        OAuthRouteDependencies(
            require_auth_session=_require_auth_session,
            oauth_callback_url=_oauth_callback_url,
            oauth_success_url=_oauth_success_url,
            frontend_profile_url=_frontend_profile_url,
            issue_auth_session=_issue_auth_session,
        )
    )
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


from app.api.routes.chat import ChatRouteDependencies, create_chat_router


app.include_router(
    create_chat_router(
        ChatRouteDependencies(
            chat_user_id=_chat_user_id,
            begin_chat_security=_begin_chat_security,
            get_agent_with_profile=get_agent_with_profile,
            enforce_conversation_limit=_enforce_conversation_limit,
            guard_conversation_creation=_guard_conversation_creation,
            reserve_chat_ticket=_reserve_chat_ticket,
            wait_for_chat_ticket=_wait_for_chat_ticket,
            completed_conversation_limit=_completed_conversation_limit,
            finish_chat_security=_CHAT_GUARD.finish,
            stage_to_route=_stage_to_route,
            get_agent=get_agent,
        )
    )
)

from app.api.routes.weather_advice import create_weather_advice_router

app.include_router(create_weather_advice_router())

# ══════════ 第三段：skin-records CRUD + reports ══════════

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


from app.api.routes.skin_records import create_skin_record_router
from app.services.skin_record_service import SkinRecordService

app.include_router(
    create_skin_record_router(
        owner_resolver=_current_skin_user_id,
        service=SkinRecordService(
            store_provider=get_store,
            save_record=save_skin_record,
            build_report=build_report,
            render_report_text=render_report_text,
        ),
    )
)


# ══════════ 前端相容層：skin-records 她的payload / profile / checkin ══════════


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
from app.api.routes.daily_checkin import create_daily_checkin_router
from app.services.daily_checkin_service import DailyCheckinService


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


app.include_router(
    create_daily_checkin_router(
        service=DailyCheckinService(),
        resolve_user_id=_resolve_checkin_uid,
    )
)


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
