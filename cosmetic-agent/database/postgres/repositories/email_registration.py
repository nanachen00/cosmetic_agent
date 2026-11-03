"""PostgreSQL verified-email registration challenges.

RECONSTRUCTED 2026-10-21, mirroring password_reset.py: only HMAC hashes of
six-digit codes are stored, codes are single use, attempts and sends are capped.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from database.postgres.connection import postgres_connection
from database.postgres.repositories.password_auth import (
    PostgresPasswordAuthRepository,
    normalize_username,
    validate_new_password,
)
from database.repositories.email_registration import (
    EmailRegistrationDispatch,
    EmailRegistrationError,
    EmailRegistrationRateLimitedError,
    InvalidEmailRegistrationCodeError,
)
from database.repositories.password_account_types import RegisteredAccount
from database.repositories.password_auth import PasswordAuthValidationError

ConnectionContextFactory = Callable[[], AbstractContextManager[Any]]

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_TTL_MINUTES = 10
_MAX_SENDS_PER_WINDOW = 3
_MAX_CODE_ATTEMPTS = 5


class EmailAlreadyRegisteredError(EmailRegistrationError):
    pass


def _random_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise PasswordAuthValidationError("Email 格式不正確")
    return email


class PostgresEmailRegistrationRepository:
    def __init__(
        self,
        *,
        secret: str,
        connection_factory: ConnectionContextFactory = postgres_connection,
        password_repository: PostgresPasswordAuthRepository | None = None,
        code_factory: Callable[[], str] = _random_code,
    ) -> None:
        if len(secret or "") < 32:
            raise ValueError("註冊驗證碼雜湊密鑰至少需要 32 個字元")
        self._secret = secret.encode("utf-8")
        self._connection_factory = connection_factory
        self._passwords = password_repository or PostgresPasswordAuthRepository(
            connection_factory=connection_factory
        )
        self._code_factory = code_factory

    def _code_hash(self, challenge_id: UUID, code: str) -> str:
        return hmac.new(
            self._secret, f"register:{challenge_id}:{code}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _lock(connection: Any, key: str) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def issue(self, *, email: str) -> EmailRegistrationDispatch:
        normalized_email = _normalize_email(email)
        code = self._code_factory()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=_CODE_TTL_MINUTES)
        with self._connection_factory() as connection:
            self._lock(connection, f"email-registration:{normalized_email}")
            if connection.execute(
                "SELECT 1 FROM user_emails WHERE lower(email) = %s", (normalized_email,)
            ).fetchone():
                raise EmailAlreadyRegisteredError("此 Email 已註冊，請直接登入或使用忘記密碼")
            recent = connection.execute(
                "SELECT count(*) AS count FROM email_registration_challenges "
                "WHERE lower(email) = %s AND created_at >= now() - interval '30 minutes'",
                (normalized_email,),
            ).fetchone()["count"]
            if recent >= _MAX_SENDS_PER_WINDOW:
                raise EmailRegistrationRateLimitedError("驗證碼寄送次數過多，請稍後再試")
            challenge_id = uuid4()
            connection.execute(
                "INSERT INTO email_registration_challenges "
                "(id, email, code_hash, max_attempts, expires_at) VALUES (%s, %s, %s, %s, %s)",
                (challenge_id, normalized_email, self._code_hash(challenge_id, code),
                 _MAX_CODE_ATTEMPTS, expires_at),
            )
        return EmailRegistrationDispatch(
            challenge_id=challenge_id,
            email=normalized_email,
            code=code,
            expires_in_seconds=_CODE_TTL_MINUTES * 60,
        )

    def consume(
        self, *, challenge_id: UUID, code: str, username: str, password: str
    ) -> RegisteredAccount:
        normalize_username(username)
        validate_new_password(password)
        failure: Exception | None = None
        email: str | None = None
        with self._connection_factory() as connection:
            self._lock(connection, f"email-registration-challenge:{challenge_id}")
            row = connection.execute(
                "SELECT email, code_hash, attempt_count, max_attempts, expires_at, consumed_at "
                "FROM email_registration_challenges WHERE id = %s FOR UPDATE",
                (challenge_id,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= datetime.now(timezone.utc)
                or row["attempt_count"] >= row["max_attempts"]
            ):
                failure = InvalidEmailRegistrationCodeError("驗證碼已失效，請重新取得")
            elif not hmac.compare_digest(
                self._code_hash(challenge_id, (code or "").strip()), row["code_hash"]
            ):
                connection.execute(
                    "UPDATE email_registration_challenges SET attempt_count = attempt_count + 1 "
                    "WHERE id = %s",
                    (challenge_id,),
                )
                failure = InvalidEmailRegistrationCodeError("驗證碼不正確")
            else:
                email = row["email"]
                connection.execute(
                    "UPDATE email_registration_challenges SET consumed_at = now() WHERE id = %s",
                    (challenge_id,),
                )
        if failure is not None:
            raise failure
        assert email is not None
        account = self._passwords.register(username=username, verified_email=email, password=password)
        return RegisteredAccount(user_id=account.user_id, username=account.username, email=email)

    def invalidate(self, *, challenge_id: UUID) -> None:
        with self._connection_factory() as connection:
            connection.execute(
                "UPDATE email_registration_challenges "
                "SET consumed_at = COALESCE(consumed_at, now()) WHERE id = %s",
                (challenge_id,),
            )
