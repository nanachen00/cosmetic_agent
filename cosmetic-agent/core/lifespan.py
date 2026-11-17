"""Compose application lifespan hooks without deprecated event APIs."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any


async def _call(handler: Callable[[], Any] | None) -> None:
    if handler is None:
        return
    result = handler()
    if inspect.isawaitable(result):
        await result


def register_lifespan(
    app: Any,
    *,
    startup: Callable[[], Any] | None = None,
    shutdown: Callable[[], Any] | None = None,
) -> None:
    """Wrap the current lifespan so independently installed features compose."""

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def composed(application: Any):
        async with previous(application):
            await _call(startup)
            try:
                yield
            finally:
                await _call(shutdown)

    app.router.lifespan_context = composed
