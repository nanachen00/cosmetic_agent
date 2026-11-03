import asyncio

import pytest

from core.admission import (
    AdmissionFull,
    AdmissionTimeout,
    ChatAdmissionController,
)


@pytest.mark.asyncio
async def test_fifo_and_queue_limit():
    controller = ChatAdmissionController(max_running=1, max_queued=1)
    first = await controller.reserve()
    second = await controller.reserve()
    with pytest.raises(AdmissionFull):
        await controller.reserve()

    waiter = asyncio.create_task(second.wait(0.5))
    await asyncio.sleep(0)
    assert not waiter.done()
    await first.release()
    assert await waiter >= 0
    await second.release()
    assert controller.active == 0
    assert controller.queued == 0


@pytest.mark.asyncio
async def test_queue_timeout_removes_reservation():
    controller = ChatAdmissionController(max_running=1, max_queued=1)
    first = await controller.reserve()
    second = await controller.reserve()
    with pytest.raises(AdmissionTimeout):
        await second.wait(0.01)
    assert controller.queued == 0
    await first.release()


@pytest.mark.asyncio
async def test_release_is_idempotent():
    controller = ChatAdmissionController(max_running=1, max_queued=0)
    ticket = await controller.reserve()
    await ticket.release()
    await ticket.release()
    assert controller.active == 0
