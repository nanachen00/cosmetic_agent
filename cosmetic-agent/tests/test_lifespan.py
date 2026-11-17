from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.lifespan import register_lifespan


def test_registered_lifespans_compose_in_stack_order() -> None:
    app = FastAPI()
    events = []

    register_lifespan(
        app,
        startup=lambda: events.append("first-start"),
        shutdown=lambda: events.append("first-stop"),
    )
    register_lifespan(
        app,
        startup=lambda: events.append("second-start"),
        shutdown=lambda: events.append("second-stop"),
    )

    with TestClient(app):
        assert events == ["first-start", "second-start"]

    assert events == [
        "first-start",
        "second-start",
        "second-stop",
        "first-stop",
    ]
