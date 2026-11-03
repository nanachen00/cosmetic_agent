"""Low-overhead OpenTelemetry tracing for BLINK.

Only operational metadata is exported. Prompts, responses, cookies, API keys,
email addresses, and GetZep context are never attached to spans.
"""
from __future__ import annotations
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

_LOGGER = logging.getLogger("blink.tracing")
_CONFIGURED = False
_TRACER: Any = None

def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _safe_name(value: Any, fallback: str = "unknown") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip()).strip("_")
    return (normalized or fallback)[:80]

def _endpoint() -> str:
    explicit = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if explicit:
        return explicit
    host = os.getenv("BETTER_STACK_LOG_INGESTING_HOST", "").strip().rstrip("/")
    if not host:
        return ""
    if not urlparse(host).scheme:
        host = f"https://{host}"
    return f"{host}/v1/traces"

def _set_state(app: Any, *, enabled: bool, reason: str, sample_rate: float) -> None:
    app.state.blink_tracing_enabled = enabled
    app.state.blink_tracing_status = {
        "enabled": enabled,
        "reason": reason,
        "sample_rate": sample_rate,
        "export": "better_stack_otlp_http" if enabled else None,
    }

def _server_request_hook(span: Any, scope: dict[str, Any]) -> None:
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return
    try:
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        request_id = headers.get(b"x-request-id", b"").decode("utf-8", errors="ignore").strip()
        if request_id:
            span.set_attribute("blink.request_id", request_id[:128])
    except Exception:
        return


def configure_tracing(app: Any) -> bool:
    global _CONFIGURED, _TRACER
    if _CONFIGURED:
        return bool(getattr(app.state, "blink_tracing_enabled", False))
    sample_rate = min(1.0, max(0.0, float(os.getenv("OTEL_TRACE_SAMPLE_RATE", "0.10"))))
    if not _enabled("OTEL_TRACING_ENABLED", default=False):
        _set_state(app, enabled=False, reason="disabled", sample_rate=sample_rate)
        return False
    endpoint = _endpoint()
    token = os.getenv("BETTER_STACK_LOG_SOURCE_TOKEN", "").strip()
    if not endpoint or not token:
        _set_state(app, enabled=False, reason="missing_endpoint_or_token", sample_rate=sample_rate)
        _LOGGER.warning("tracing_disabled_missing_better_stack_configuration")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "blink-backend"),
            "service.version": os.getenv("APP_RELEASE", "unversioned"),
            "deployment.environment": os.getenv("APP_ENV", "development"),
        })
        provider = TracerProvider(resource=resource, sampler=ParentBased(root=TraceIdRatioBased(sample_rate)))
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=float(os.getenv("OTEL_EXPORT_TIMEOUT_SECONDS", "3")),
        )
        provider.add_span_processor(BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            schedule_delay_millis=5000,
            max_export_batch_size=256,
            export_timeout_millis=3000,
        ))
        trace.set_tracer_provider(provider)
        _TRACER = provider.get_tracer("blink.backend")
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="healthz,api/v1/live,api/v1/ready,metrics,api/auth",
        server_request_hook=_server_request_hook,
        )
        optional = (
            ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
            ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
            ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
            ("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
        )
        for module_name, class_name in optional:
            try:
                module = __import__(module_name, fromlist=[class_name])
                getattr(module, class_name)().instrument(tracer_provider=provider)
            except Exception as exc:
                _LOGGER.warning("tracing_dependency_instrumentation_failed dependency=%s error=%s", class_name, type(exc).__name__)
        _CONFIGURED = True
        _set_state(app, enabled=True, reason="configured", sample_rate=sample_rate)
        _LOGGER.info("tracing_configured service=%s sample_rate=%s", os.getenv("OTEL_SERVICE_NAME", "blink-backend"), sample_rate)
        return True
    except Exception as exc:
        _set_state(app, enabled=False, reason=f"initialization_failed:{type(exc).__name__}", sample_rate=sample_rate)
        _LOGGER.exception("tracing_initialization_failed")
        return False

def record_completed_stage(*, pipeline: str, stage: str, status: str, duration: float) -> None:
    if _TRACER is None or duration < 0:
        return
    try:
        from opentelemetry.trace import Status, StatusCode
        end_ns = time.time_ns()
        start_ns = max(0, end_ns - int(duration * 1_000_000_000))
        safe_status = _safe_name(status)
        span = _TRACER.start_span(
            f"blink.{_safe_name(pipeline)}.{_safe_name(stage)}",
            start_time=start_ns,
            attributes={
                "blink.pipeline": _safe_name(pipeline),
                "blink.stage": _safe_name(stage),
                "blink.status": safe_status,
                "blink.duration_ms": round(duration * 1000, 2),
            },
        )
        if safe_status not in {"success", "ok", "completed"}:
            span.set_status(Status(StatusCode.ERROR, safe_status))
        span.end(end_time=end_ns)
    except Exception:
        return
