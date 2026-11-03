"""PostgreSQL password credential repository (argon2id).

RECONSTRUCTED 2026-10-21 from call sites. Hashes stay compatible with
password_reset.py and postgres_password_login.py, which already write
``argon2id`` hashes into ``password_credentials``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from database.postgres.connection import postgres_connection
from database.repositories.password_auth import (
    AccountLockedError,
    InvalidCredentialsError,
    PasswordAccount,
    PasswordAlreadySetError,
    PasswordAuthValidationError,
    PasswordNotSetError,
    UsernameUnavailableError,
)

ConnectionContextFactory = Callable[[], AbstractContextManager[Any]]

USERNAME_PATTERN = re.compile(r"^[a-z0-9._]{4,20}$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
# A dummy hash keeps timing similar for unknown usernames.
_DUMMY_HASH = PasswordHasher().hash("blink-dummy-password-0")


def normalize_username(value: str) -> str:
    username = (value or "").strip().lower().removeprefix("@")
    if not USERNAME_PATTERN.fullmatch(username):
        raise PasswordAuthValidationError(
            "請輸入 4–20 個字元，只能使用小寫英文字母、數字、句點與底線"
        )
    if username.startswith(".") or username.endswith("."):
        raise PasswordAuthValidationError("會員帳號不能以句點開頭或結尾")
    if ".." in username:
        raise PasswordAuthValidationError("會員帳號不能包含連續句點")
    return username


def validate_new_password(value: str) -> str:
    password = value or ""
    if not 8 <= len(password) <= 72:
        raise PasswordAuthValidationError("會員密碼須為 8–72 個字元")
    if not any(character.isalpha() for character in password):
        raise PasswordAuthValidationError("會員密碼至少需要一個英文字母")
    if not any(character.isdigit() for character in password):
        raise PasswordAuthValidationError("會員密碼至少需要一個數字")
    return password


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise PasswordAuthValidationError("Email 格式不正確")
    return email


class PostgresPasswordAuthRepository:
    def __init__(
        self,
        *,
        connection_factory: ConnectionContextFactory = postgres_connection,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._hasher = password_hasher or PasswordHasher()

    @staticmethod
    def _lock(connection: Any, key: str) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # ── registration ───────────────────────────────────────────────
    def register(self, *, username: str, verified_email: str, password: str) -> PasswordAccount:
        normalized_username = normalize_username(username)
        normalized_email = normalize_email(verified_email)
        password_hash = self._hasher.hash(validate_new_password(password))

        with self._connection_factory() as connection:
            for key in sorted((f"username:{normalized_username}", f"email:{normalized_email}")):
                self._lock(connection, key)
            if connection.execute(
                "SELECT 1 FROM usernames WHERE lower(username) = %s", (normalized_username,)
            ).fetchone():
                raise UsernameUnavailableError("這個會員帳號已被使用")
            if connection.execute(
                "SELECT 1 FROM user_emails WHERE lower(email) = %s", (normalized_email,)
            ).fetchone():
                raise PasswordAuthValidationError("此 Email 已註冊，請直接登入或使用忘記密碼")
            user_id = connection.execute(
                "INSERT INTO users (status) VALUES ('active') RETURNING id"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO usernames (user_id, username) VALUES (%s, %s)",
                (user_id, normalized_username),
            )
            connection.execute(
                "INSERT INTO user_emails (user_id, email, is_primary, verified_at) "
                "VALUES (%s, %s, true, now())",
                (user_id, normalized_email),
            )
            connection.execute(
                "INSERT INTO password_credentials (user_id, password_hash, hash_algorithm) "
                "VALUES (%s, %s, 'argon2id')",
                (user_id, password_hash),
            )
        return PasswordAccount(user_id=user_id, username=normalized_username)

    # ── login ──────────────────────────────────────────────────────
    def authenticate(self, *, username: str, password: str) -> PasswordAccount:
        try:
            normalized_username = normalize_username(username)
        except PasswordAuthValidationError as error:
            raise InvalidCredentialsError("會員帳號或密碼不正確") from error

        failure: Exception | None = None
        account: PasswordAccount | None = None
        with self._connection_factory() as connection:
            row = connection.execute(
                """
                SELECT u.id AS user_id, un.username, pc.password_hash,
                       pc.failed_attempts, pc.locked_until
                FROM usernames AS un
                JOIN users AS u ON u.id = un.user_id
                LEFT JOIN password_credentials AS pc ON pc.user_id = u.id
                WHERE lower(un.username) = %s
                  AND u.status = 'active' AND u.deleted_at IS NULL
                FOR UPDATE OF u
                """,
                (normalized_username,),
            ).fetchone()
            if row is None:
                self._verify(_DUMMY_HASH, password or "")
                raise InvalidCredentialsError("會員帳號或密碼不正確")
            if row["password_hash"] is None:
                raise PasswordNotSetError("尚未設定會員密碼")
            now = datetime.now(timezone.utc)
            if row["locked_until"] is not None and row["locked_until"] > now:
                raise AccountLockedError("登入失敗次數過多，請 15 分鐘後再試")

            if self._verify(row["password_hash"], password or ""):
                connection.execute(
                    "UPDATE password_credentials SET failed_attempts = 0, locked_until = NULL "
                    "WHERE user_id = %s",
                    (row["user_id"],),
                )
                if self._hasher.check_needs_rehash(row["password_hash"]):
                    connection.execute(
                        "UPDATE password_credentials SET password_hash = %s WHERE user_id = %s",
                        (self._hasher.hash(password), row["user_id"]),
                    )
                account = PasswordAccount(user_id=row["user_id"], username=row["username"])
            else:
                attempts = int(row["failed_attempts"] or 0) + 1
                locked_until = (
                    now + timedelta(minutes=LOCK_MINUTES)
                    if attempts >= MAX_FAILED_ATTEMPTS
                    else None
                )
                connection.execute(
                    "UPDATE password_credentials SET failed_attempts = %s, locked_until = %s "
                    "WHERE user_id = %s",
                    (0 if locked_until else attempts, locked_until, row["user_id"]),
                )
                failure = (
                    AccountLockedError("登入失敗次數過多，請 15 分鐘後再試")
                    if locked_until
                    else InvalidCredentialsError("會員帳號或密碼不正確")
                )
        # Raise after commit so failed-attempt counters persist.
        if failure is not None:
            raise failure
        assert account is not None
        return account

    # ── password management ────────────────────────────────────────
    def set_initial_password(self, *, user_id: UUID, new_password: str) -> None:
        password_hash = self._hasher.hash(validate_new_password(new_password))
        with self._connection_factory() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (user_id,),
            ).fetchone():
                raise InvalidCredentialsError("找不到會員資料")
            if connection.execute(
                "SELECT 1 FROM password_credentials WHERE user_id = %s", (user_id,)
            ).fetchone():
                raise PasswordAlreadySetError("會員密碼已設定，請使用變更密碼")
            connection.execute(
                "INSERT INTO password_credentials (user_id, password_hash, hash_algorithm) "
                "VALUES (%s, %s, 'argon2id')",
                (user_id, password_hash),
            )

    def change_password(self, *, user_id: UUID, current_password: str, new_password: str) -> None:
        new_hash = self._hasher.hash(validate_new_password(new_password))
        failure: Exception | None = None
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT password_hash, failed_attempts, locked_until "
                "FROM password_credentials WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if row is None:
                raise PasswordNotSetError("尚未設定會員密碼")
            now = datetime.now(timezone.utc)
            if row["locked_until"] is not None and row["locked_until"] > now:
                raise AccountLockedError("驗證失敗次數過多，請 15 分鐘後再試")
            if not self._verify(row["password_hash"], current_password or ""):
                attempts = int(row["failed_attempts"] or 0) + 1
                locked_until = now + timedelta(minutes=LOCK_MINUTES) if attempts >= MAX_FAILED_ATTEMPTS else None
                connection.execute(
                    "UPDATE password_credentials SET failed_attempts = %s, locked_until = %s "
                    "WHERE user_id = %s",
                    (0 if locked_until else attempts, locked_until, user_id),
                )
                failure = (
                    AccountLockedError("驗證失敗次數過多，請 15 分鐘後再試")
                    if locked_until
                    else InvalidCredentialsError("原密碼不正確")
                )
            else:
                connection.execute(
                    """
                    UPDATE password_credentials
                    SET password_hash = %s, hash_algorithm = 'argon2id',
                        password_changed_at = now(), failed_attempts = 0, locked_until = NULL
                    WHERE user_id = %s
                    """,
                    (new_hash, user_id),
                )
        if failure is not None:
            raise failure
