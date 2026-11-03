from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.guest_session import create_guest_session_router


class FakeSigner:
    def __init__(self, verified_user_id: str | None = None) -> None:
        self.verified_user_id = verified_user_id
        self.verify_calls: list[str] = []
        self.issue_calls = 0

    def verify(self, token: str) -> str | None:
        self.verify_calls.append(token)
        return self.verified_user_id

    def issue(self) -> tuple[str, str]:
        self.issue_calls += 1
        return "guest-issued", "signed-cookie"


def _make_client(signer: FakeSigner, base_url: str = "http://localhost") -> TestClient:
    app = FastAPI()
    app.include_router(
        create_guest_session_router(
            signer_provider=lambda: signer,
            cookie_name="blink_guest_session",
            cookie_ttl_seconds=3600,
        )
    )
    return TestClient(app, base_url=base_url)


def test_existing_signed_guest_is_reused_without_new_cookie() -> None:
    signer = FakeSigner(verified_user_id="guest-existing")
    client = _make_client(signer)
    client.cookies.set("blink_guest_session", "existing-token")
    response = client.post("/api/auth/guest-session")

    assert response.status_code == 200
    assert response.json() == {"user_id": "guest-existing"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert signer.verify_calls == ["existing-token"]
    assert signer.issue_calls == 0


def test_missing_guest_cookie_is_issued_with_local_cookie_policy() -> None:
    signer = FakeSigner()
    response = _make_client(signer).post("/api/auth/guest-session")

    assert response.status_code == 200
    assert response.json() == {"user_id": "guest-issued"}
    set_cookie = response.headers["set-cookie"]
    assert "blink_guest_session=signed-cookie" in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie
    assert signer.issue_calls == 1


def test_non_local_guest_cookie_remains_secure() -> None:
    signer = FakeSigner()
    response = _make_client(signer, base_url="https://blinkai.tw").post(
        "/api/auth/guest-session"
    )

    assert "Secure" in response.headers["set-cookie"]
