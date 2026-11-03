"""Bounded CrossEncoder HTTP service; cancelled callers never free busy GPU slots."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StrictInt, StrictStr

logger = logging.getLogger("blink.reranker")


@dataclass(frozen=True)
class Settings:
    model_name: str = "BAAI/bge-reranker-v2-m3"
    device: str = "cuda"
    max_documents: int = 64
    max_document_chars: int = 4000
    max_concurrent: int = 1
    max_queued: int = 8
    queue_timeout_seconds: float = 2.0
    inference_timeout_seconds: float = 30.0
    batch_size: int = 8
    release: str = "knowledge-reranker-dev"

    def __post_init__(self) -> None:
        if not self.model_name or not self.device:
            raise ValueError("model_name_and_device_required")
        if min(self.max_documents, self.max_document_chars, self.max_concurrent, self.batch_size) < 1:
            raise ValueError("positive_reranker_limits_required")
        if self.max_queued < 0:
            raise ValueError("nonnegative_queue_limit_required")
        for value in (self.queue_timeout_seconds, self.inference_timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("finite_positive_reranker_timeout_required")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model_name=os.getenv("RERANKER_MODEL_NAME", cls.model_name).strip(),
            device=os.getenv("RERANKER_DEVICE", cls.device).strip(),
            max_documents=max(1, int(os.getenv("RERANKER_MAX_DOCUMENTS", "64"))),
            max_document_chars=max(256, int(os.getenv("RERANKER_MAX_DOCUMENT_CHARS", "4000"))),
            max_concurrent=max(1, int(os.getenv("RERANKER_MAX_CONCURRENT", "1"))),
            max_queued=max(0, int(os.getenv("RERANKER_MAX_QUEUED", "8"))),
            queue_timeout_seconds=max(.1, float(os.getenv("RERANKER_QUEUE_TIMEOUT_SECONDS", "2.0"))),
            inference_timeout_seconds=max(.1, float(os.getenv("RERANKER_INFERENCE_TIMEOUT_SECONDS", "30.0"))),
            batch_size=max(1, int(os.getenv("RERANKER_BATCH_SIZE", "8"))),
            release=os.getenv("APP_RELEASE", cls.release),
        )


SETTINGS = Settings.from_env()
# Keep the old module-level names for deployment tooling that imports them.
MODEL_NAME = SETTINGS.model_name
DEVICE = SETTINGS.device
MAX_DOCUMENTS = SETTINGS.max_documents
MAX_DOCUMENT_CHARS = SETTINGS.max_document_chars
MAX_CONCURRENT = SETTINGS.max_concurrent
QUEUE_TIMEOUT_SECONDS = SETTINGS.queue_timeout_seconds
BATCH_SIZE = SETTINGS.batch_size
APP_RELEASE = SETTINGS.release


class RerankRequest(BaseModel):
    model: Optional[StrictStr] = None
    query: StrictStr = Field(min_length=1, max_length=2000)
    documents: list[StrictStr] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    top_n: StrictInt = Field(default=min(5, MAX_DOCUMENTS), ge=1, le=MAX_DOCUMENTS)
    return_documents: bool = False


class QueueFull(Exception):
    pass


class InferenceTimedOut(Exception):
    pass


class BoundedInference:
    """A bounded waiting room plus slots owned by tasks, not HTTP callers.

    Python cannot kill an in-flight inference thread. An HTTP cancellation or
    deadline therefore detaches its result, but the slot stays occupied until
    the real thread exits. Detached tasks are strongly held and exceptions read.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.active = 0
        self.queued = 0
        self.closed = False
        self._condition = asyncio.Condition()
        self._tasks: set[asyncio.Task[Any]] = set()

    async def _acquire(self) -> None:
        async with self._condition:
            if self.closed:
                raise QueueFull()
            if self.active < self.settings.max_concurrent and self.queued == 0:
                self.active += 1
                return
            if self.queued >= self.settings.max_queued:
                raise QueueFull()
            self.queued += 1
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self.closed or self.active < self.settings.max_concurrent
                    ),
                    timeout=self.settings.queue_timeout_seconds,
                )
                if self.closed:
                    raise QueueFull()
                self.active += 1
            except asyncio.TimeoutError as exc:
                raise QueueFull() from exc
            finally:
                self.queued -= 1

    async def _release(self) -> None:
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()

    async def _execute(self, work: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(work)
        finally:
            await self._release()

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def run(self, work: Callable[[], Any]) -> Any:
        await self._acquire()
        # There is no cancellation point between acquiring and task ownership.
        task = asyncio.create_task(self._execute(work))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self.settings.inference_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise InferenceTimedOut() from exc

    async def close(self) -> None:
        async with self._condition:
            self.closed = True
            self._condition.notify_all()
        # Graceful process shutdown must not unload the model under running work.
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


def _load_model(settings: Settings) -> Any:
    # Lazy import makes contract tests independent of model files and GPU packages.
    import torch
    from sentence_transformers import CrossEncoder

    if settings.device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("configured_accelerator_unavailable")
    return CrossEncoder(settings.model_name, device=settings.device, max_length=512)


def _accelerator_info(model: Any, settings: Settings) -> dict[str, Any]:
    """Inspect the loaded device; do not infer GPU use just from configuration."""
    actual = getattr(model, "device", None)
    if actual is None:
        actual = next(model.model.parameters()).device
    actual_device = str(actual)
    if settings.device.startswith("cuda") and not actual_device.startswith("cuda"):
        raise RuntimeError("configured_accelerator_not_in_use")
    result: dict[str, Any] = {"device": actual_device, "backend": "cpu"}
    if actual_device.startswith("cuda"):
        import torch

        index = getattr(actual, "index", None)
        if index is None:
            index = torch.cuda.current_device()
        result.update(
            backend="rocm" if torch.version.hip else "cuda",
            name=torch.cuda.get_device_name(index),
        )
    return result


def _predict(model: Any, settings: Settings, query: str, documents: list[str]) -> list[float]:
    pairs = [(query, document) for document in documents]
    values = model.predict(
        pairs, batch_size=settings.batch_size, show_progress_bar=False, convert_to_numpy=True
    )
    if hasattr(values, "reshape"):
        values = values.reshape(-1).tolist()
    scores = [float(value) for value in values]
    if len(scores) != len(documents) or not all(math.isfinite(value) for value in scores):
        raise ValueError("invalid_model_scores")
    return scores


def _accepts_model(requested: Optional[str], configured: str) -> bool:
    if requested is None:
        return True
    # No substring matching: an arbitrary model must not silently use another one.
    aliases = {configured}
    if configured.rstrip("/").rsplit("/", 1)[-1] == "bge-reranker-v2-m3":
        aliases.update({"BAAI/bge-reranker-v2-m3", "bge-reranker-v2-m3"})
    return requested.strip() in aliases


def create_app(
    settings: Optional[Settings] = None,
    model_loader: Optional[Callable[[Settings], Any]] = None,
) -> FastAPI:
    config = settings or SETTINGS
    loader = model_loader or _load_model

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        state = application.state
        state.gate = BoundedInference(config)
        state.model = None
        state.ready = False
        state.accelerator = None
        try:
            model = await asyncio.to_thread(loader, config)
            accelerator = _accelerator_info(model, config)
            await asyncio.to_thread(
                _predict, model, config, "保濕", ["神經醯胺有助於皮膚屏障照護"]
            )
            state.model = model
            state.accelerator = accelerator
            state.ready = True
        except Exception as exc:
            logger.error("reranker_initialization_failed error_type=%s", type(exc).__name__)
            # Do not keep a partially loaded accelerator model alive in the
            # suspended lifespan generator after an unsuccessful warmup.
            model = None
        try:
            yield
        finally:
            state.ready = False
            await state.gate.close()
            state.model = None

    application = FastAPI(title="BLINK reranker", lifespan=lifespan)
    application.state.ready = False
    application.state.requests_total = 0
    application.state.errors_total = 0
    application.state.last_latency_ms = None

    @application.get("/livez")
    async def livez():
        return {"status": "ok", "release": config.release}

    @application.get("/readyz")
    async def readyz():
        if not application.state.ready:
            raise HTTPException(503, detail={"ready": False, "error": "reranker_not_ready"})
        return {
            "ready": True,
            "model": config.model_name,
            "device": application.state.accelerator["device"],
            "accelerator": application.state.accelerator,
            "max_concurrent": config.max_concurrent,
            "max_queued": config.max_queued,
            "max_documents": config.max_documents,
            "release": config.release,
        }

    @application.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": config.model_name, "object": "model"}]}

    @application.get("/metrics")
    async def metrics():
        state = application.state
        gate = getattr(state, "gate", None)
        return {
            "requests_total": state.requests_total,
            "errors_total": state.errors_total,
            "last_latency_ms": state.last_latency_ms,
            "ready": state.ready,
            "active": gate.active if gate else 0,
            "queued": gate.queued if gate else 0,
        }

    @application.post("/v1/rerank")
    async def rerank(payload: RerankRequest):
        state = application.state
        state.requests_total += 1
        started = time.perf_counter()
        try:
            if not state.ready:
                raise HTTPException(503, detail="reranker_not_ready")
            if not _accepts_model(payload.model, config.model_name):
                raise HTTPException(422, detail="unsupported_model")
            query = payload.query.strip()
            documents = [document.strip() for document in payload.documents]
            if not query or not documents or len(documents) > config.max_documents:
                raise HTTPException(422, detail="invalid_query_or_document_count")
            if payload.top_n > config.max_documents:
                raise HTTPException(422, detail="invalid_top_n")
            if any(not document or len(document) > config.max_document_chars for document in documents):
                raise HTTPException(422, detail="invalid_document")
            try:
                scores = await state.gate.run(lambda: _predict(state.model, config, query, documents))
            except QueueFull:
                raise HTTPException(429, detail="reranker_queue_full", headers={"Retry-After": "1"})
            except InferenceTimedOut:
                raise HTTPException(504, detail="reranker_inference_timeout")
            except Exception as exc:
                logger.error("reranker_inference_failed error_type=%s", type(exc).__name__)
                raise HTTPException(503, detail="reranker_inference_failed")
            top_n = min(payload.top_n, len(documents))
            ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:top_n]
            results = []
            for index, score in ranked:
                result: dict[str, Any] = {"index": index, "relevance_score": score}
                if payload.return_documents:
                    result["document"] = {"text": documents[index]}
                results.append(result)
            return {
                "id": f"rerank-{time.time_ns()}",
                "model": config.model_name,
                "results": results,
                "usage": {"documents": len(documents)},
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        except (HTTPException, asyncio.CancelledError):
            state.errors_total += 1
            raise
        finally:
            state.last_latency_ms = round((time.perf_counter() - started) * 1000, 3)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("RERANKER_HOST", "127.0.0.1"), port=int(os.getenv("RERANKER_PORT", "8006")))
