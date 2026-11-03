from __future__ import annotations

import asyncio
import time

import pytest

from app.api.routes import chat


class _Ticket:
    def __init__(self) -> None:
        self.released = False

    async def release(self) -> None:
        self.released = True


@pytest.mark.asyncio
async def test_disconnected_stream_releases_ticket_and_security(monkeypatch) -> None:
    producer = asyncio.create_task(asyncio.sleep(0))
    ticket = _Ticket()
    finished = []
    observed = []

    async def finish(guard_ticket) -> None:
        finished.append(guard_ticket)

    monkeypatch.setattr(
        chat,
        "observe_pipeline_stage",
        lambda pipeline, stage, started_at: observed.append((pipeline, stage, started_at)),
    )

    started_at = time.perf_counter()
    await chat._release_stream_resources_when_done(
        producer,
        ticket,
        "guard-1",
        finish,
        release_started_at=started_at,
    )

    assert ticket.released is True
    assert finished == ["guard-1"]
    assert observed == [("chat", "cancel_release", started_at)]
