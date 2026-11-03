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

import functools
import hashlib
import json
import logging
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
        Counter,
        Gauge,
        Histogram,
        generate_latest,
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

DEPENDENCY_ERRORS = Counter(
    "blink_dependency_errors_total",
    "Total dependency errors",
    ("dependency", "operation"),
)

ACTIVE_SESSIONS = Gauge(
    "blink_active_sessions",
    "Current in-memory Root Agent sessions",
)


_ALLOWED_DEPENDENCIES = {
    "getzep",
    "neo4j",
    "openai",
    "taide",
    "frontend",
    "unknown",
}


class JsonFormatter(logging.Formatter):
    """每行輸出一個 JSON object，方便 grep、jq、Loki 收集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": REQUEST_ID.get(),
        }

        for key in (
            "event",
            "user_id_hash",
            "conversation_id",
            "agent",
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
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(
                record.exc_info
            )

        return json.dumps(
            payload,
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

    @app.middleware("http")
    async def observability_middleware(
        request: Request,
        call_next: Callable[..., Any],
    ) -> Response:
        request_id = (
            request.headers.get("X-Request-ID")
            or uuid.uuid4().hex
        )[:128]
        token = REQUEST_ID.set(request_id)
        started_at = time.perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
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
                    request.url.path,
                ),
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

    @app.get(
        "/metrics",
        include_in_schema=False,
    )
    def metrics_endpoint() -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get(
        "/api/v1/ready",
        include_in_schema=False,
    )
    def readiness_endpoint() -> JSONResponse:
        checks: dict[str, str] = {}

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

        return JSONResponse(
            status_code=(
                200
                if is_ready
                else 503
            ),
            content={
                "status": (
                    "ready"
                    if is_ready
                    else "not_ready"
                ),
                "dependencies": checks,
            },
        )
