"""PostgreSQL single-use password reset challenge implementation."""

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

from argon2 import PasswordHasher

from database.postgres.connection import postgres_connection
from database.repositories.password_auth import PasswordAuthValidationError
from database.repositories.password_reset import (
    InvalidPasswordResetCodeError,
    PasswordResetDispatch,
    PasswordResetError,
    PasswordResetRateLimitedError,
    PasswordResetUnavailableError,
)

ConnectionContextFactory = Callable[
    [], AbstractContextManager[Any]
]
CodeFactory = Callable[[], str]

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_TTL_MINUTES = 10
_SEND_WINDOW_MINUTES = 30
_MAX_SENDS_PER_WINDOW = 3
_MAX_CODE_ATTEMPTS = 5


def _random_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise PasswordResetUnavailableError("無法處理密碼重設請求")
    return email


def _validate_new_password(value: str) -> str:
    password = value or ""
    if not 8 <= len(password) <= 72:
        raise PasswordAuthValidationError("會員密碼須為 8–72 個字元")
    if not any(character.isalpha() for character in password):
        raise PasswordAuthValidationError("會員密碼至少需要一個英文字母")
    if not any(character.isdigit() for character in password):
        raise PasswordAuthValidationError("會員密碼至少需要一個數字")
    return password


class PostgresPasswordResetRepository:
    """Persist only HMAC code hashes; plaintext codes exist only for dispatch."""

    def __init__(
        self,
        *,
        secret: str,
        connection_factory: ConnectionContextFactory = postgres_connection,
        password_hasher: PasswordHasher | None = None,
        code_factory: CodeFactory = _random_code,
    ) -> None:
        if len(secret or "") < 32:
            raise ValueError("密碼重設雜湊密鑰至少需要 32 個字元")
        self._secret = secret.encode("utf-8")
        self._connection_factory = connection_factory
        self._hasher = password_hasher or PasswordHasher()
        self._code_factory = code_factory

    def _code_hash(self, challenge_id: UUID, code: str) -> str:
        return hmac.new(
            self._secret,
            f"{challenge_id}:{code}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _lock(connection: Any, key: str) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (key,),
        )

    def issue(self, *, email: str) -> PasswordResetDispatch:
        normalized_email = _normalize_email(email)
        code = self._code_factory()
        if len(code) != 6 or not code.isdigit():
            raise ValueError("驗證碼產生器必須回傳六位數字")
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=_CODE_TTL_MINUTES)

        with self._connection_factory() as connection:
            self._lock(connection, f"password-reset-email:{normalized_email}")
            owner = connection.execute(
                """
                SELECT ue.user_id
                FROM user_emails AS ue
                JOIN users AS u ON u.id = ue.user_id
                WHERE lower(ue.email) = %s
                  AND ue.verified_at IS NOT NULL
                  AND u.status = 'active'
                  AND u.deleted_at IS NULL
                FOR UPDATE OF ue, u
                """,
                (normalized_email,),
            ).fetchone()
            if owner is None:
                raise PasswordResetUnavailableError("無法處理密碼重設請求")

            recent_count = connection.execute(
                "SELECT count(*) AS count FROM password_reset_challenges "
                "WHERE user_id = %s AND created_at >= now() - interval '30 minutes'",
                (owner["user_id"],),
            ).fetchone()["count"]
            if recent_count >= _MAX_SENDS_PER_WINDOW:
                raise PasswordResetRateLimitedError("驗證碼寄送次數過多，請稍後再試")

            challenge_id = uuid4()
            connection.execute(
                """
                INSERT INTO password_reset_challenges (
                    id, user_id, code_hash, max_attempts, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    challenge_id,
                    owner["user_id"],
                    self._code_hash(challenge_id, code),
                    _MAX_CODE_ATTEMPTS,
                    expires_at,
                ),
            )

        return PasswordResetDispatch(
            challenge_id=challenge_id,
            email=normalized_email,
            code=code,
            expires_in_seconds=_CODE_TTL_MINUTES * 60,
        )

    def consume(
        self,
        *,
        challenge_id: UUID,
        code: str,
        new_password: str,
    ) -> UUID:
        password_hash = self._hasher.hash(_validate_new_password(new_password))
        failure: PasswordResetError | None = None
        user_id: UUID | None = None

        with self._connection_factory() as connection:
            self._lock(connection, f"password-reset-challenge:{challenge_id}")
            challenge = connection.execute(
                """
                SELECT id, user_id, code_hash, attempt_count, max_attempts,
                       expires_at, consumed_at
                FROM password_reset_challenges
                WHERE id = %s
                FOR UPDATE
                """,
                (challenge_id,),
            ).fetchone()
            invalid = (
                challenge is None
                or challenge["consumed_at"] is not None
                or challenge["expires_at"] <= datetime.now(timezone.utc)
                or challenge["attempt_count"] >= challenge["max_attempts"]
            )
            if invalid:
                failure = InvalidPasswordResetCodeError("驗證碼已失效，請重新取得")
            elif not hmac.compare_digest(
                self._code_hash(challenge_id, (code or "").strip()),
                challenge["code_hash"],
            ):
                connection.execute(
                    "UPDATE password_reset_challenges "
                    "SET attempt_count = attempt_count + 1 WHERE id = %s",
                    (challenge_id,),
                )
                failure = InvalidPasswordResetCodeError("驗證碼不正確")
            else:
                user_id = challenge["user_id"]
                connection.execute(
                    """
                    INSERT INTO password_credentials (
                        user_id, password_hash, hash_algorithm,
                        password_changed_at, failed_attempts, locked_until
                    ) VALUES (%s, %s, 'argon2id', now(), 0, NULL)
                    ON CONFLICT (user_id) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        hash_algorithm = 'argon2id',
                        password_changed_at = now(),
                        failed_attempts = 0,
                        locked_until = NULL
                    """,
                    (user_id, password_hash),
                )
                connection.execute(
                    "UPDATE password_reset_challenges "
                    "SET consumed_at = now() WHERE id = %s AND consumed_at IS NULL",
                    (challenge_id,),
                )

        if failure:
            raise failure
        if user_id is None:
            raise InvalidPasswordResetCodeError("驗證碼已失效，請重新取得")
        return user_id

    def invalidate(self, *, challenge_id: UUID) -> None:
        with self._connection_factory() as connection:
            connection.execute(
                "UPDATE password_reset_challenges "
                "SET consumed_at = COALESCE(consumed_at, now()) WHERE id = %s",
                (challenge_id,),
            )
