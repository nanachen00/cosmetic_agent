"""
BLINK AI Observability
======================

提供：
- JSON 結構化日誌
- X-Request-ID 串接
- FastAPI HTTP request metrics
- Root Agent turn metrics / logging
- 外部依賴錯誤計數
- /metrics 與 /api/v1/ready

不記錄完整 user message、prompt、API key、email 或 GetZep context。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        multiprocess,
    )
    PROMETHEUS_AVAILABLE = True

except ImportError:
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

    class _NoopMetric:
        def labels(self, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, amount: float = 1.0) -> None:
            return None

        def observe(self, amount: float) -> None:
            return None

        def set(self, value: float) -> None:
            return None

    def Counter(*args: Any, **kwargs: Any) -> _NoopMetric:
        return _NoopMetric()

    def Gauge(*args: Any, **kwargs: Any) -> _NoopMetric:
        return _NoopMetric()

    def Histogram(*args: Any, **kwargs: Any) -> _NoopMetric:
        return _NoopMetric()

    def generate_latest() -> bytes:
        return (
            b"# prometheus_client is not installed\n"
        )


REQUEST_ID: ContextVar[str] = ContextVar(
    "blink_request_id",
    default="-",
)


HTTP_REQUESTS = Counter(
    "blink_http_requests_total",
    "Total HTTP requests",
    ("method", "route", "status"),
)

HTTP_LATENCY = Histogram(
    "blink_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
    buckets=(
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        20.0,
        40.0,
    ),
)

CHAT_TURNS = Counter(
    "blink_chat_turns_total",
    "Total Root Agent chat turns",
    ("result",),
)

AGENT_ROUTES = Counter(
    "blink_agent_routes_total",
    "Total routed Agent turns",
    ("agent", "stage"),
)

AGENT_LATENCY = Histogram(
    "blink_agent_turn_duration_seconds",
    "Root Agent turn duration in seconds",
    ("agent",),
    buckets=(
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        20.0,
        40.0,
        80.0,
    ),
)

PIPELINE_STAGE_LATENCY = Histogram(
    "blink_pipeline_stage_duration_seconds",
    "Pipeline stage duration in seconds",
    ("pipeline", "stage", "status"),
    buckets=(
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        20.0,
        40.0,
        80.0,
        120.0,
        180.0,
    ),
)


CHAT_ACTIVE_REQUESTS = Gauge(
    "blink_chat_active_requests",
    "Chat generations currently holding an execution slot",
    multiprocess_mode="livesum",
)

CHAT_QUEUED_REQUESTS = Gauge(
    "blink_chat_queued_requests",
    "Chat generations waiting for an execution slot",
    multiprocess_mode="livesum",
)

CHAT_OVERLOADS = Counter(
    "blink_chat_overload_total",
    "Chat requests rejected or expired before execution",
    ("reason",),
)


EVENT_LOOP_LAG = Histogram(
    "blink_event_loop_lag_seconds",
    "Observed asyncio event-loop scheduling lag in seconds",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0),
)
EVENT_LOOP_LAG_CURRENT = Gauge(
    "blink_event_loop_lag_current_seconds",
    "Most recently observed asyncio event-loop scheduling lag in seconds",
    multiprocess_mode="livemax",
)

PIPELINE_STAGE_TOTAL = Counter(
    "blink_pipeline_stage_total",
    "Total pipeline stage executions",
    ("pipeline", "stage", "status"),
)


DEPENDENCY_ERRORS = Counter(
    "blink_dependency_errors_total",
    "Total dependency errors",
    ("dependency", "operation"),
)

ACTIVE_SESSIONS = Gauge(
    "blink_active_sessions",
    "Current in-memory Root Agent sessions",
    multiprocess_mode="livesum",
)


_ALLOWED_DEPENDENCIES = {
    "getzep",
    "neo4j",
    "openai",
    "taide",
    "frontend",
    "unknown",
}



_SENSITIVE_KEYS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "email",
    "phone",
    "mobile",
    "user_message",
    "chat_message",
    "conversation_content",
    "prompt",
    "completion",
    "response",
    "image",
    "base64",
    "request_body",
    "body",
})

_SECRET_PATTERNS = (
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?886[-\s]?|0)9\d{2}(?:[-\s]?\d){6}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}(?:\.[a-zA-Z0-9_-]{10,})?\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)"), r"\1=[REDACTED]"),
)


def _redact_text(value: Any, max_length: int = 2048) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:max_length]


def _scrub_value(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"

    normalized_key = str(key).strip().lower().replace("-", "_")
    if normalized_key not in {"user_id_hash", "conversation_id_hash"}:
        if (
            normalized_key in _SENSITIVE_KEYS
            or normalized_key.endswith(("_token", "_secret", "_password", "_api_key"))
        ):
            return "[REDACTED]"

    if isinstance(value, Mapping):
        return {
            str(child_key)[:80]: _scrub_value(
                child_value,
                str(child_key),
                depth + 1,
            )
            for child_key, child_value in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple, set)):
        return [_scrub_value(item, key, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return _redact_text(value, max_length=512)
    return value


def _scrub_sentry_event(event: dict[str, Any], hint: Any) -> Optional[dict[str, Any]]:
    try:
        cleaned = _scrub_value(event)
        if not isinstance(cleaned, dict):
            return None

        request = cleaned.get("request")
        if isinstance(request, dict):
            request.pop("data", None)
            request.pop("cookies", None)
            request.pop("query_string", None)
            headers = request.get("headers")
            if isinstance(headers, dict):
                request["headers"] = {
                    name: value
                    for name, value in headers.items()
                    if str(name).lower() in {
                        "content-type",
                        "user-agent",
                        "x-request-id",
                    }
                }

        cleaned.pop("user", None)
        return cleaned
    except Exception:
        # Privacy-safe failure mode: drop the event rather than send raw data.
        return None


def _configure_error_tracking() -> bool:
    dsn = os.getenv("BETTER_STACK_ERRORS_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("APP_ENV", "development"),
        release=os.getenv("APP_RELEASE") or None,
        sample_rate=1.0,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        send_default_pii=False,
        max_request_body_size="never",
        max_breadcrumbs=10,
        before_send=_scrub_sentry_event,
        integrations=[
            FastApiIntegration(),
            LoggingIntegration(level=None, event_level=None),
        ],
        shutdown_timeout=1.0,
    )
    return True


ERROR_TRACKING_ENABLED = _configure_error_tracking()


class JsonFormatter(logging.Formatter):
    """每行輸出一個 JSON object，方便 grep、jq、Loki 收集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_text(record.getMessage()),
            "request_id": REQUEST_ID.get(),
        }

        for key in (
            "event",
            "user_id_hash",
            "conversation_id",
            "agent",
            "pipeline",
            "stage",
            "duration_ms",
            "status",
            "dependency",
            "operation",
            "error_type",
            "http_method",
            "http_route",
            "http_status",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = (
                    hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
                    if key == "conversation_id"
                    else _scrub_value(value, key)
                )

        if record.exc_info:
            payload["exception"] = _redact_text(
                self.formatException(record.exc_info)
            )

        return json.dumps(
            _scrub_value(payload),
            ensure_ascii=False,
            default=str,
        )


def configure_logging(
    level: int = logging.INFO,
) -> logging.Logger:
    """只設定 blink logger，不破壞既有 uvicorn logger。"""

    logger = logging.getLogger("blink")
    logger.setLevel(level)
    logger.propagate = False

    if not any(
        getattr(handler, "_blink_json_handler", False)
        for handler in logger.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._blink_json_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)

    log_source_token = os.getenv("BETTER_STACK_LOG_SOURCE_TOKEN", "").strip()
    log_ingesting_host = os.getenv("BETTER_STACK_LOG_INGESTING_HOST", "").strip()
    if log_source_token and log_ingesting_host and not any(
        getattr(handler, "_blink_better_stack_handler", False)
        for handler in logger.handlers
    ):
        try:
            from logtail import LogtailHandler

            remote_handler = LogtailHandler(
                source_token=log_source_token,
                host=log_ingesting_host,
                buffer_capacity=500,
                flush_interval=1,
                check_interval=0.1,
                timeout=2,
                raise_exceptions=False,
                drop_extra_events=True,
            )
            remote_handler.setLevel(logging.WARNING)
            remote_handler.setFormatter(JsonFormatter())
            remote_handler._blink_better_stack_handler = True  # type: ignore[attr-defined]
            logger.addHandler(remote_handler)
        except Exception:
            # Telemetry is always fail-open; stdout logging remains available.
            pass

    return logger


LOGGER = configure_logging()

if not PROMETHEUS_AVAILABLE:
    LOGGER.warning(
        "prometheus_client is not installed; metrics are disabled",
        extra={
            "event": "metrics_disabled",
            "status": "degraded",
        },
    )


def _generate_metrics_payload() -> bytes:
    """Render the default registry or aggregate all worker shards."""

    multiproc_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if not (PROMETHEUS_AVAILABLE and multiproc_dir):
        return generate_latest()

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return generate_latest(registry)


def _mark_prometheus_process_dead() -> None:
    """Remove live-gauge shards for a gracefully stopped worker."""

    if not PROMETHEUS_AVAILABLE:
        return
    if not os.getenv("PROMETHEUS_MULTIPROC_DIR", "").strip():
        return
    try:
        multiprocess.mark_process_dead(os.getpid())
    except Exception:
        LOGGER.exception("Failed to mark Prometheus worker process dead")


def hash_identifier(value: Any) -> str:
    """將 user id 雜湊後寫入 log，避免直接暴露帳號識別資料。"""

    text = str(value or "").strip()
    if not text:
        return "-"

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()[:12]


def _safe_label(
    value: Any,
    default: str = "unknown",
    max_length: int = 80,
) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default

    text = re.sub(
        r"[^a-z0-9_./{}:-]+",
        "_",
        text,
    )
    return text[:max_length] or default



_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_request_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def _stage_to_agent(stage: Any) -> str:
    normalized = _safe_label(stage)

    if normalized.startswith("skin_"):
        return "skin_tracking"
    if "knowledge" in normalized:
        return "knowledge"
    if any(
        marker in normalized
        for marker in (
            "product",
            "recommend",
            "ranking",
        )
    ):
        return "product"
    if "experience" in normalized:
        return "experience"
    if normalized.startswith("guard"):
        return "guard"
    if "clarif" in normalized:
        return "clarification"

    return "orchestrator"


def log_event(
    event: str,
    message: str,
    level: int = logging.INFO,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    LOGGER.log(
        level,
        message,
        extra={
            "event": event,
            **fields,
        },
        exc_info=exc_info,
    )


def record_dependency_error(
    dependency: str,
    operation: str,
    error: BaseException,
    **fields: Any,
) -> None:
    normalized_dependency = _safe_label(dependency)
    if normalized_dependency not in _ALLOWED_DEPENDENCIES:
        normalized_dependency = "unknown"

    normalized_operation = _safe_label(
        operation,
        max_length=40,
    )

    DEPENDENCY_ERRORS.labels(
        dependency=normalized_dependency,
        operation=normalized_operation,
    ).inc()

    log_event(
        event="dependency_error",
        message=(
            f"Dependency operation failed: "
            f"{normalized_dependency}/{normalized_operation}"
        ),
        level=logging.ERROR,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
        dependency=normalized_dependency,
        operation=normalized_operation,
        error_type=type(error).__name__,
        status="error",
        **fields,
    )



def _normalize_metric_label(value: Any, fallback: str = "unknown") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "").strip()).strip("_")
    return (normalized or fallback)[:80]

def observe_pipeline_stage(
    pipeline: str,
    stage: str,
    started_at: float,
    *,
    status: str = "success",
) -> float:
    """Record one pipeline stage without logging prompts or user content."""
    duration = max(0.0, time.perf_counter() - started_at)
    safe_pipeline = _normalize_metric_label(pipeline, "unknown")
    safe_stage = _normalize_metric_label(stage, "unknown")
    safe_status = _normalize_metric_label(status, "unknown")

    PIPELINE_STAGE_LATENCY.labels(
        pipeline=safe_pipeline,
        stage=safe_stage,
        status=safe_status,
    ).observe(duration)
    PIPELINE_STAGE_TOTAL.labels(
        pipeline=safe_pipeline,
        stage=safe_stage,
        status=safe_status,
    ).inc()

    log_event(
        event="pipeline_stage_completed",
        message="Pipeline stage completed",
        level=logging.WARNING if duration >= 10.0 else logging.INFO,
        pipeline=safe_pipeline,
        stage=safe_stage,
        status=safe_status,
        duration_ms=round(duration * 1000, 2),
    )
    try:
        from core.tracing import record_completed_stage

        record_completed_stage(
            pipeline=safe_pipeline,
            stage=safe_stage,
            status=safe_status,
            duration=duration,
        )
    except Exception:
        pass
    return duration


def set_chat_admission_state(active: int, queued: int) -> None:
    """Publish bounded-queue state without recording user content."""
    CHAT_ACTIVE_REQUESTS.set(max(0, int(active)))
    CHAT_QUEUED_REQUESTS.set(max(0, int(queued)))


def observe_chat_overload(reason: str) -> None:
    """Count and log queue saturation or queue timeout events."""
    safe_reason = _normalize_metric_label(reason, "unknown")
    CHAT_OVERLOADS.labels(reason=safe_reason).inc()
    log_event(
        event="chat_overload",
        message="Chat request could not enter an execution slot",
        level=logging.WARNING,
        reason=safe_reason,
        status="overloaded",
    )


def monitor_agent_turn(
    function: Callable[..., dict],
) -> Callable[..., dict]:
    """
    RootAgentOrchestrator.process decorator。

    只記錄 user id hash、conversation id、stage、route 與耗時，
    不記錄完整對話內容。
    """

    @functools.wraps(function)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> dict:
        started_at = time.perf_counter()
        memory = getattr(self, "memory", None)

        user_id_hash = hash_identifier(
            getattr(memory, "user_id", "")
        )
        conversation_id = str(
            getattr(memory, "conversation_id", "-")
            or "-"
        )[:80]

        try:
            result = function(
                self,
                *args,
                **kwargs,
            )

            stage = _safe_label(
                (
                    result.get("stage")
                    if isinstance(result, dict)
                    else "invalid_result"
                )
            )
            agent = _stage_to_agent(stage)
            duration = time.perf_counter() - started_at

            CHAT_TURNS.labels(
                result="success"
            ).inc()
            AGENT_ROUTES.labels(
                agent=agent,
                stage=stage,
            ).inc()
            AGENT_LATENCY.labels(
                agent=agent
            ).observe(duration)

            log_event(
                event="chat_completed",
                message="Root Agent turn completed",
                user_id_hash=user_id_hash,
                conversation_id=conversation_id,
                agent=agent,
                stage=stage,
                duration_ms=round(duration * 1000, 2),
                status="success",
            )

            return result

        except Exception as error:
            duration = time.perf_counter() - started_at

            CHAT_TURNS.labels(
                result="error"
            ).inc()
            AGENT_LATENCY.labels(
                agent="orchestrator"
            ).observe(duration)

            log_event(
                event="chat_failed",
                message="Root Agent turn failed",
                level=logging.ERROR,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
                user_id_hash=user_id_hash,
                conversation_id=conversation_id,
                agent="orchestrator",
                stage="error",
                duration_ms=round(duration * 1000, 2),
                status="error",
                error_type=type(error).__name__,
            )

            raise

    return wrapper


async def _monitor_event_loop_lag(interval: float) -> None:
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        lag = max(0.0, now - expected)
        EVENT_LOOP_LAG.observe(lag)
        EVENT_LOOP_LAG_CURRENT.set(lag)
        expected = now + interval


def _install_event_loop_lag_monitor(app: Any) -> None:
    async def start_monitor() -> None:
        interval = max(0.05, float(os.getenv("EVENT_LOOP_LAG_INTERVAL_SECONDS", "0.5")))
        app.state.blink_event_loop_lag_task = asyncio.create_task(
            _monitor_event_loop_lag(interval),
            name="blink-event-loop-lag",
        )

    async def stop_monitor() -> None:
        task = getattr(app.state, "blink_event_loop_lag_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app.add_event_handler("startup", start_monitor)
    app.add_event_handler("shutdown", stop_monitor)


def _metrics_authorized(request: Request) -> bool:
    """Allow local scrapers or an explicitly authenticated remote scraper."""

    expected_token = os.getenv("METRICS_BEARER_TOKEN", "").strip()
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if (
        expected_token
        and scheme.lower() == "bearer"
        and hmac.compare_digest(supplied_token.strip(), expected_token)
    ):
        return True

    if os.getenv("METRICS_ALLOW_LOOPBACK", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False

    client = getattr(request, "client", None)
    client_host = str(getattr(client, "host", "") or "").strip()
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return client_host.lower() == "localhost"


def install_observability(
    app: Any,
    session_count_provider: Optional[Callable[[], int]] = None,
    readiness_checks: Optional[
        Mapping[str, Callable[[], bool]]
    ] = None,
) -> None:
    """安裝 FastAPI middleware、/metrics 與 readiness endpoint。"""

    if getattr(
        app.state,
        "blink_observability_installed",
        False,
    ):
        return

    app.state.blink_observability_installed = True
    _install_event_loop_lag_monitor(app)

    async def mark_prometheus_process_dead() -> None:
        _mark_prometheus_process_dead()

    app.add_event_handler(
        "shutdown",
        mark_prometheus_process_dead,
    )

    @app.middleware("http")
    async def observability_middleware(
        request: Request,
        call_next: Callable[..., Any],
    ) -> Response:
        request_id = _normalize_request_id(
            request.headers.get("X-Request-ID")
        )
        token = REQUEST_ID.set(request_id)
        started_at = time.perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            release = os.getenv("APP_RELEASE")
            if release:
                response.headers["X-App-Release"] = release
            return response

        except Exception as error:
            log_event(
                event="http_unhandled_error",
                message="Unhandled HTTP exception",
                level=logging.ERROR,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
                http_method=request.method,
                http_route=request.url.path,
                http_status=500,
                status="error",
                error_type=type(error).__name__,
            )
            raise

        finally:
            duration = time.perf_counter() - started_at
            route_object = request.scope.get("route")
            route = _safe_label(
                getattr(
                    route_object,
                    "path",
                    None,
                ) or "unmatched",
                max_length=100,
            )
            method = _safe_label(
                request.method,
                max_length=12,
            ).upper()

            HTTP_REQUESTS.labels(
                method=method,
                route=route,
                status=str(status_code),
            ).inc()
            HTTP_LATENCY.labels(
                method=method,
                route=route,
            ).observe(duration)

            if session_count_provider is not None:
                try:
                    ACTIVE_SESSIONS.set(
                        max(
                            0,
                            int(session_count_provider()),
                        )
                    )
                except Exception:
                    pass

            log_event(
                event="http_request",
                message="HTTP request completed",
                http_method=method,
                http_route=route,
                http_status=status_code,
                duration_ms=round(duration * 1000, 2),
                status=(
                    "success"
                    if status_code < 500
                    else "error"
                ),
            )

            REQUEST_ID.reset(token)

    @app.get("/livez", include_in_schema=False)
    @app.get("/api/v1/live", include_in_schema=False)
    def liveness_endpoint() -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
        )

    @app.get(
        "/metrics",
        include_in_schema=False,
    )
    def metrics_endpoint(request: Request) -> Response:
        if not _metrics_authorized(request):
            return JSONResponse(
                status_code=404,
                content={"detail": "Not Found"},
            )
        return Response(
            content=_generate_metrics_payload(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get(
        "/api/v1/ready",
        include_in_schema=False,
    )
    @app.get("/readyz", include_in_schema=False)
    def readiness_endpoint() -> JSONResponse:
        checks: dict[str, str] = {"application": "ok"}

        for name, check in (
            readiness_checks
            or {}
        ).items():
            try:
                checks[name] = (
                    "ok"
                    if bool(check())
                    else "unavailable"
                )
            except Exception:
                checks[name] = "error"

        is_ready = all(
            value == "ok"
            for value in checks.values()
        )

        content: dict[str, Any] = {
            "status": "ready" if is_ready else "not_ready",
            "dependencies": checks,
        }
        release = os.getenv("APP_RELEASE", "").strip()
        if release:
            content["release"] = release
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content=content,
        )
