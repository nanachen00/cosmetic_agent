"""Low-overhead usage ledger for AI models and paid tools.

Only numeric usage and operational metadata are stored. Prompts, responses,
Zep memories, URLs and API keys are deliberately excluded.
"""
from __future__ import annotations

import atexit
import asyncio
import contextvars
import importlib.util
import inspect
import json
import os
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Callable

from database.postgres.connection import postgres_connection

logger = logging.getLogger(__name__)
_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10000)
_started = False
_installed = False
_lock = threading.Lock()
_zep_depth: contextvars.ContextVar[int] = contextvars.ContextVar("usage_zep_depth", default=0)


def _request_id() -> str | None:
    try:
        from core.observability import REQUEST_ID
        value = REQUEST_ID.get()
        return str(value) if value else None
    except Exception:
        return None


def _number(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _operation_from_stack() -> str:
    routes = (
        ("agents/root/orchestrator.py", "chat_orchestration"),
        ("intent_parser.py", "intent_routing"),
        ("response_composer.py", "response_composition"),
        ("knowledge_sub_agent.py", "knowledge_answer"),
        ("product_web_search.py", "product_research"),
        ("experience_parser.py", "experience_parse"),
        ("input_guard.py", "safety_guard"),
        ("conversation_title_service.py", "conversation_title"),
        ("skin_tracking", "skin_tracking"),
    )
    frame = sys._getframe(2)
    for _ in range(13):
        filename = frame.f_code.co_filename.replace("\\", "/")
        for needle, operation in routes:
            if needle in filename:
                return operation
        frame = frame.f_back
        if frame is None:
            break
    return "model_call"


def _worker() -> None:
    while True:
        first = _queue.get()
        batch = [first]
        deadline = time.monotonic() + 0.5
        while len(batch) < 100 and time.monotonic() < deadline:
            try:
                batch.append(_queue.get(timeout=max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                break
        try:
            with postgres_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO api_usage_events
                        (occurred_at, category, provider, service, operation, status,
                         request_id, input_tokens, cached_input_tokens, output_tokens,
                         total_tokens, quantity, unit_type, latency_ms, error_type, metadata)
                        VALUES (NOW(), %(category)s, %(provider)s, %(service)s,
                         %(operation)s, %(status)s, %(request_id)s, %(input_tokens)s,
                         %(cached_input_tokens)s, %(output_tokens)s, %(total_tokens)s,
                         %(quantity)s, %(unit_type)s, %(latency_ms)s, %(error_type)s,
                         %(metadata)s::jsonb)""",
                        batch,
                    )
                connection.commit()
        except Exception:
            logger.exception("Usage ledger batch write failed")
        finally:
            for _ in batch:
                _queue.task_done()


def _ensure_worker() -> None:
    global _started
    if _started:
        return
    with _lock:
        if not _started:
            threading.Thread(target=_worker, name="usage-ledger", daemon=True).start()
            _started = True


def record_usage(
    *, category: str, provider: str, service: str = "", operation: str = "",
    status: str = "success", input_tokens: int = 0, cached_input_tokens: int = 0,
    output_tokens: int = 0, total_tokens: int = 0, quantity: float = 1,
    unit_type: str = "operation", latency_ms: float | None = None,
    error_type: str | None = None, metadata: dict[str, Any] | None = None,
) -> None:
    """Queue a sanitized event. Never blocks or breaks the user request."""
    if not os.getenv("DATABASE_URL") or importlib.util.find_spec("psycopg") is None:
        return
    _ensure_worker()
    safe_metadata = {str(k)[:64]: str(v)[:160] for k, v in (metadata or {}).items()}
    event = {
        "category": category, "provider": provider, "service": service or "",
        "operation": operation or "", "status": status, "request_id": _request_id(),
        "input_tokens": max(0, _number(input_tokens)),
        "cached_input_tokens": max(0, _number(cached_input_tokens)),
        "output_tokens": max(0, _number(output_tokens)),
        "total_tokens": max(0, _number(total_tokens)),
        "quantity": max(0, float(quantity or 0)), "unit_type": unit_type,
        "latency_ms": latency_ms, "error_type": error_type,
        "metadata": json.dumps(safe_metadata, ensure_ascii=False),
    }
    try:
        _queue.put_nowait(event)
    except queue.Full:
        logger.warning("Usage ledger queue full; dropping event")


def flush_usage(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


def _usage_values(result: Any) -> tuple[int, int, int, int]:
    usage = _get(result, "usage")
    if usage is None:
        response = _get(result, "response")
        usage = _get(response, "usage")
    inp = _number(_get(usage, "input_tokens", _get(usage, "prompt_tokens", 0)))
    out = _number(_get(usage, "output_tokens", _get(usage, "completion_tokens", 0)))
    total = _number(_get(usage, "total_tokens", inp + out), inp + out)
    details = _get(usage, "input_tokens_details", _get(usage, "prompt_tokens_details"))
    cached = _number(_get(details, "cached_tokens", 0))
    return inp, cached, out, total


def _record_ai(result: Any, *, model: str, operation: str, started: float,
               status: str = "success", error: BaseException | None = None) -> None:
    inp, cached, out, total = _usage_values(result)
    actual_model = str(_get(result, "model", model) or model)
    response = _get(result, "response")
    actual_model = str(_get(response, "model", actual_model) or actual_model)
    record_usage(
        category="ai_model", provider="openai", service=actual_model,
        operation=operation, status=status, input_tokens=inp,
        cached_input_tokens=cached, output_tokens=out, total_tokens=total,
        quantity=1, unit_type="request", latency_ms=(time.perf_counter()-started)*1000,
        error_type=type(error).__name__ if error else None,
        metadata={"usage_source": "provider_response" if total else "missing"},
    )


class _SyncStreamProxy:
    def __init__(self, stream: Any, *, model: str, operation: str, started: float):
        self._stream, self._model, self._operation, self._started = stream, model, operation, started
        self._recorded = False
    def _finish(self, value: Any = None, error: BaseException | None = None) -> None:
        if not self._recorded:
            self._recorded = True
            _record_ai(value, model=self._model, operation=self._operation,
                       started=self._started, status="error" if error else "success", error=error)
    def __iter__(self):
        latest = None
        try:
            for item in self._stream:
                usage = _get(item, "usage")
                if usage is not None or _get(item, "type") == "response.completed": latest = item
                yield item
            self._finish(latest)
        except BaseException as exc:
            self._finish(latest, exc); raise
    def __enter__(self):
        if hasattr(self._stream, "__enter__"): self._stream.__enter__()
        return self
    def __exit__(self, *args):
        try: return self._stream.__exit__(*args) if hasattr(self._stream, "__exit__") else None
        finally:
            if args and args[0]: self._finish(None, args[1])
    def __getattr__(self, name: str) -> Any: return getattr(self._stream, name)


class _AsyncStreamProxy:
    def __init__(self, stream: Any, *, model: str, operation: str, started: float):
        self._stream, self._model, self._operation, self._started = stream, model, operation, started
        self._recorded = False
    def _finish(self, value: Any = None, error: BaseException | None = None) -> None:
        if not self._recorded:
            self._recorded = True
            _record_ai(value, model=self._model, operation=self._operation,
                       started=self._started, status="error" if error else "success", error=error)
    async def __aiter__(self):
        latest = None
        try:
            async for item in self._stream:
                usage = _get(item, "usage")
                if usage is not None or _get(item, "type") == "response.completed": latest = item
                yield item
            self._finish(latest)
        except BaseException as exc:
            self._finish(latest, exc); raise
    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"): await self._stream.__aenter__()
        return self
    async def __aexit__(self, *args):
        try: return await self._stream.__aexit__(*args) if hasattr(self._stream, "__aexit__") else None
        finally:
            if args and args[0]: self._finish(None, args[1])
    def __getattr__(self, name: str) -> Any: return getattr(self._stream, name)


def _patch_openai() -> None:
    targets: list[tuple[Any, str, bool]] = []
    try:
        from openai.resources.chat.completions import Completions, AsyncCompletions
        targets += [(Completions, "chat", False), (AsyncCompletions, "chat", True)]
    except Exception:
        logger.exception("OpenAI chat usage patch unavailable")
    try:
        from openai.resources.responses.responses import Responses, AsyncResponses
        targets += [(Responses, "responses", False), (AsyncResponses, "responses", True)]
    except Exception:
        logger.exception("OpenAI responses usage patch unavailable")
    for cls, kind, is_async in targets:
        original = getattr(cls, "create", None)
        if original is None or getattr(original, "_blink_usage_wrapped", False): continue
        if is_async:
            async def async_create(self, *args, __orig=original, __kind=kind, **kwargs):
                started, operation = time.perf_counter(), _operation_from_stack()
                model = str(kwargs.get("model", "unknown"))
                if kwargs.get("stream") and __kind == "chat":
                    kwargs.setdefault("stream_options", {"include_usage": True})
                try:
                    result = await __orig(self, *args, **kwargs)
                except BaseException as exc:
                    _record_ai(None, model=model, operation=operation, started=started, status="error", error=exc); raise
                if kwargs.get("stream"): return _AsyncStreamProxy(result, model=model, operation=operation, started=started)
                _record_ai(result, model=model, operation=operation, started=started); return result
            async_create._blink_usage_wrapped = True
            setattr(cls, "create", async_create)
        else:
            def sync_create(self, *args, __orig=original, __kind=kind, **kwargs):
                started, operation = time.perf_counter(), _operation_from_stack()
                model = str(kwargs.get("model", "unknown"))
                if kwargs.get("stream") and __kind == "chat":
                    kwargs.setdefault("stream_options", {"include_usage": True})
                try:
                    result = __orig(self, *args, **kwargs)
                except BaseException as exc:
                    _record_ai(None, model=model, operation=operation, started=started, status="error", error=exc); raise
                if kwargs.get("stream"): return _SyncStreamProxy(result, model=model, operation=operation, started=started)
                _record_ai(result, model=model, operation=operation, started=started); return result
            sync_create._blink_usage_wrapped = True
            setattr(cls, "create", sync_create)


def _patch_zep() -> None:
    try:
        from tools.getzep.zep_memory_adapter import ZepMemoryAdapter
    except Exception:
        logger.exception("GetZep usage patch unavailable"); return
    names = (
        "load_profile", "get", "set", "save", "to_summary", "record_product_experience",
        "list_conversations", "rename_conversation", "delete_conversation", "touch_conversation",
        "add_user_message", "add_assistant_message", "add_turn_to_zep", "get_context_block",
        "get_history", "clear_thread",
    )
    for name in names:
        original = getattr(ZepMemoryAdapter, name, None)
        if original is None or getattr(original, "_blink_usage_wrapped", False): continue
        if inspect.iscoroutinefunction(original):
            async def aw(self, *args, __orig=original, __name=name, **kwargs):
                depth = _zep_depth.get(); token = _zep_depth.set(depth + 1); started = time.perf_counter()
                try:
                    result = await __orig(self, *args, **kwargs)
                    if depth == 0: record_usage(category="memory", provider="getzep", service="thread-memory", operation=__name, latency_ms=(time.perf_counter()-started)*1000)
                    return result
                except BaseException as exc:
                    if depth == 0: record_usage(category="memory", provider="getzep", service="thread-memory", operation=__name, status="error", latency_ms=(time.perf_counter()-started)*1000, error_type=type(exc).__name__)
                    raise
                finally: _zep_depth.reset(token)
            aw._blink_usage_wrapped = True; setattr(ZepMemoryAdapter, name, aw)
        else:
            def sw(self, *args, __orig=original, __name=name, **kwargs):
                depth = _zep_depth.get(); token = _zep_depth.set(depth + 1); started = time.perf_counter()
                try:
                    result = __orig(self, *args, **kwargs)
                    if depth == 0: record_usage(category="memory", provider="getzep", service="thread-memory", operation=__name, latency_ms=(time.perf_counter()-started)*1000)
                    return result
                except BaseException as exc:
                    if depth == 0: record_usage(category="memory", provider="getzep", service="thread-memory", operation=__name, status="error", latency_ms=(time.perf_counter()-started)*1000, error_type=type(exc).__name__)
                    raise
                finally: _zep_depth.reset(token)
            sw._blink_usage_wrapped = True; setattr(ZepMemoryAdapter, name, sw)


def install_usage_tracking() -> None:
    global _installed
    if _installed:
        return
    if not os.getenv("DATABASE_URL") or importlib.util.find_spec("psycopg") is None:
        logger.warning("BLINK usage tracking disabled: psycopg is unavailable")
        return
    _installed = True
    _ensure_worker()
    _patch_openai()
    _patch_zep()
    logger.info("BLINK usage tracking enabled for OpenAI/Terra and GetZep")


atexit.register(flush_usage)
