from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.routes.welcome import create_welcome_router
from app.services.welcome_service import WelcomeService


class FakeAgent:
    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id

    def get_welcome(self) -> dict[str, str]:
        return {"message": "welcome", "owner_id": self.owner_id}


def _make_client(calls: list[tuple[str, str]]) -> TestClient:
    def resolve_owner(request: Request, user_id: str) -> str:
        calls.append((request.url.path, user_id))
        return f"owner:{user_id}"

    app = FastAPI()
    app.include_router(
        create_welcome_router(
            owner_resolver=resolve_owner,
            welcome_service=WelcomeService(FakeAgent),
        )
    )
    return TestClient(app)


def test_welcome_route_preserves_user_id_and_response() -> None:
    calls: list[tuple[str, str]] = []
    response = _make_client(calls).get(
        "/api/v1/welcome",
        params={"user_id": "visitor-1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "message": "welcome",
        "owner_id": "owner:visitor-1",
    }
    assert calls == [("/api/v1/welcome", "visitor-1")]


def test_welcome_route_keeps_anonymous_default() -> None:
    calls: list[tuple[str, str]] = []
    response = _make_client(calls).get("/api/v1/welcome")

    assert response.status_code == 200
    assert response.json()["owner_id"] == "owner:anonymous"
    assert calls == [("/api/v1/welcome", "anonymous")]
