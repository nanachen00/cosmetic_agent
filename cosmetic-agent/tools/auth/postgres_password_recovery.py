"""PostgreSQL-backed public password recovery service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID, uuid4

from database.postgres.repositories.password_reset import PostgresPasswordResetRepository
from database.repositories.password_reset import (
    PasswordResetError,
    PasswordResetUnavailableError,
)
from tools.auth.password_reset_mailer import (
    PasswordResetMailer,
    SmtpPasswordResetMailer,
)


@dataclass(frozen=True, slots=True)
class PasswordRecoveryRequest:
    request_id: UUID
    expires_in_seconds: int


class PasswordRecoveryDeliveryError(PasswordResetError):
    """Raised after a newly-created challenge is invalidated on mail failure."""


class PostgresPasswordRecoveryService:
    def __init__(
        self,
        *,
        repository: PostgresPasswordResetRepository,
        mailer: PasswordResetMailer,
    ) -> None:
        self._repository = repository
        self._mailer = mailer

    def request_code(self, *, email: str) -> PasswordRecoveryRequest:
        try:
            dispatch = self._repository.issue(email=email)
        except PasswordResetUnavailableError:
            # Keep the public response shape identical for unknown addresses.
            return PasswordRecoveryRequest(request_id=uuid4(), expires_in_seconds=600)

        try:
            self._mailer.send_code(
                recipient=dispatch.email,
                code=dispatch.code,
                expires_in_minutes=dispatch.expires_in_seconds // 60,
            )
        except Exception as error:
            self._repository.invalidate(challenge_id=dispatch.challenge_id)
            raise PasswordRecoveryDeliveryError("驗證信寄送失敗") from error

        return PasswordRecoveryRequest(
            request_id=dispatch.challenge_id,
            expires_in_seconds=dispatch.expires_in_seconds,
        )

    def confirm_code(
        self,
        *,
        request_id: UUID,
        code: str,
        new_password: str,
    ) -> UUID:
        return self._repository.consume(
            challenge_id=request_id,
            code=code,
            new_password=new_password,
        )


def create_postgres_password_recovery_service() -> PostgresPasswordRecoveryService:
    secret = os.getenv("PASSWORD_RESET_SECRET", "")
    return PostgresPasswordRecoveryService(
        repository=PostgresPasswordResetRepository(secret=secret),
        mailer=SmtpPasswordResetMailer(),
    )
