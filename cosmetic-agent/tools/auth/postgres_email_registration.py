"""Verified-email registration service.

RECONSTRUCTED 2026-10-21; mirrors tools/auth/postgres_password_recovery.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID, uuid4

from database.postgres.repositories.email_registration import (
    EmailAlreadyRegisteredError,
    PostgresEmailRegistrationRepository,
)
from database.repositories.email_registration import EmailRegistrationError
from database.repositories.password_account_types import RegisteredAccount
from tools.auth.password_reset_mailer import PasswordResetMailer, SmtpPasswordResetMailer


@dataclass(frozen=True, slots=True)
class EmailRegistrationRequest:
    request_id: UUID
    expires_in_seconds: int


class EmailRegistrationDeliveryError(EmailRegistrationError):
    pass


class PostgresEmailRegistrationService:
    def __init__(
        self,
        *,
        repository: PostgresEmailRegistrationRepository,
        mailer: PasswordResetMailer,
    ) -> None:
        self._repository = repository
        self._mailer = mailer

    def request_code(self, *, email: str) -> EmailRegistrationRequest:
        try:
            dispatch = self._repository.issue(email=email)
        except EmailAlreadyRegisteredError:
            # Same public response for registered addresses (no account enumeration).
            return EmailRegistrationRequest(request_id=uuid4(), expires_in_seconds=600)
        try:
            self._mailer.send_code(
                recipient=dispatch.email,
                code=dispatch.code,
                expires_in_minutes=dispatch.expires_in_seconds // 60,
            )
        except Exception as error:
            self._repository.invalidate(challenge_id=dispatch.challenge_id)
            raise EmailRegistrationDeliveryError("驗證信寄送失敗") from error
        return EmailRegistrationRequest(
            request_id=dispatch.challenge_id,
            expires_in_seconds=dispatch.expires_in_seconds,
        )

    def confirm_code(
        self, *, request_id: UUID, code: str, username: str, password: str
    ) -> RegisteredAccount:
        return self._repository.consume(
            challenge_id=request_id, code=code, username=username, password=password
        )


def create_postgres_email_registration_service() -> PostgresEmailRegistrationService:
    secret = os.getenv("EMAIL_REGISTRATION_SECRET", "") or os.getenv("PASSWORD_RESET_SECRET", "")
    return PostgresEmailRegistrationService(
        repository=PostgresEmailRegistrationRepository(secret=secret),
        mailer=SmtpPasswordResetMailer(
            subject="BLINK AI 會員註冊驗證碼",
            introduction="歡迎加入 BLINK AI！你的註冊驗證碼是：",
        ),
    )
