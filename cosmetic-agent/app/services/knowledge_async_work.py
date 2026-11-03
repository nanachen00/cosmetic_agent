"""Keep a knowledge request's permit until its blocking work actually exits."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


async def run_shielded_worker(
    function: Callable[..., Any],
    *args: Any,
    on_complete: Callable[[], Any],
    registry: set,
    **kwargs: Any,
) -> Any:
    async def work():
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        finally:
            released = on_complete()
            if inspect.isawaitable(released):
                await released

    task = asyncio.create_task(work())
    registry.add(task)

    def finished(done):
        registry.discard(done)
        # A disconnected client is no longer awaiting this task's exception.
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


class KnowledgeReadBusy(RuntimeError):
    def __init__(self):
        super().__init__("knowledge_graph_read_busy")


class KnowledgeReadGate:
    def __init__(self, concurrency: int, wait_seconds: float = 0.25):
        self._slots = asyncio.Semaphore(max(1, int(concurrency)))
        self._pending = set()
        self._wait_seconds = max(0.01, float(wait_seconds))

    async def run(self, function, *args, **kwargs):
        try:
            await asyncio.wait_for(self._slots.acquire(), self._wait_seconds)
        except asyncio.TimeoutError as error:
            raise KnowledgeReadBusy() from error
        return await run_shielded_worker(
            function, *args, on_complete=self._slots.release,
            registry=self._pending, **kwargs,
        )
