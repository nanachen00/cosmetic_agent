"""Contract check for the isolated public password-recovery routes."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.password_recovery import (
    register_password_recovery_routes,
    router,
)


class InvalidCodeError(RuntimeError):
    pass


class RateLimitedError(RuntimeError):
    pass


class PasswordValidationError(RuntimeError):
    pass


class RecoveryError(RuntimeError):
    pass


@dataclass
class RequestResult:
    request_id: UUID
    expires_in_seconds: int = 600


class FakeService:
    def __init__(self) -> None:
        self.user_id = uuid4()

    def request_code(self, *, email: str) -> RequestResult:
        assert email == "member@example.test"
        return RequestResult(request_id=uuid4())

    def confirm_code(self, *, request_id: UUID, code: str, new_password: str) -> UUID:
        assert isinstance(request_id, UUID)
        assert code == "123456"
        assert new_password == "BlinkNew1234"
        return self.user_id


def main() -> None:
    service = FakeService()
    issued_sessions: list[dict[str, str]] = []

    def issue_auth_session(**values):
        issued_sessions.append(values)

    register_password_recovery_routes(
        recovery_service_factory=lambda: service,
        issue_auth_session=issue_auth_session,
        invalid_code_error=InvalidCodeError,
        rate_limited_error=RateLimitedError,
        password_validation_error=PasswordValidationError,
        recovery_error=RecoveryError,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    request_response = client.post(
        "/api/auth/password/recovery/request",
        json={"email": "member@example.test"},
    )
    assert request_response.status_code == 202
    assert request_response.json()["ok"] is True

    confirm_response = client.post(
        "/api/auth/password/recovery/confirm",
        json={
            "request_id": str(uuid4()),
            "code": "123456",
            "new_password": "BlinkNew1234",
        },
    )
    assert confirm_response.status_code == 200
    assert confirm_response.json() == {
        "ok": True,
        "user_id": str(service.user_id),
    }
    assert issued_sessions[0]["user_id"] == str(service.user_id)
    assert issued_sessions[0]["auth_method"] == "email_reset"
    print("request_202=true confirm_200=true auth_session_issued=true")


if __name__ == "__main__":
    main()
