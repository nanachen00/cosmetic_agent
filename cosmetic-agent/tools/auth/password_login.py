"""Password-login domain errors, validation rules and service factory.

RECONSTRUCTED 2026-10-21. The original module was omitted from the source
export. Its public names are reproduced from api/api_server.py,
app/api/routes/oauth.py and tools/auth/postgres_password_login.py.
Validation messages match the live frontend (app/login/setup-username).

The legacy Neo4j-backed implementation is intentionally not recreated:
docs/architecture/DATA_OWNERSHIP.md states PostgreSQL is the only account
store once AUTH_STORAGE_BACKEND=postgres.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

USERNAME_PATTERN = re.compile(r"^[a-z0-9._]{4,20}$")
USERNAME_CHANGE_COOLDOWN = timedelta(days=7)
SUPPORTED_OAUTH_PROVIDERS = frozenset({"google", "line"})


class PasswordAuthError(RuntimeError):
    """Base class; routes map unknown subclasses to HTTP 500."""


class AccountNotFoundError(PasswordAuthError):
    pass


class InvalidCredentialsError(PasswordAuthError):
    pass


class LoginRateLimitedError(PasswordAuthError):
    pass


class PasswordValidationError(PasswordAuthError, ValueError):
    pass


class PasswordAlreadySetError(PasswordAuthError):
    pass


class PasswordNotSetError(PasswordAuthError):
    pass


class UsernameValidationError(PasswordAuthError, ValueError):
    pass


class UsernameAlreadyExistsError(PasswordAuthError):
    pass


class UsernameAlreadySetError(PasswordAuthError):
    pass


class UsernameChangeCooldownError(PasswordAuthError):
    pass


class LastLoginMethodError(PasswordAuthError):
    """Raised when unlinking would leave an account without any login method."""


class EmailIdentityConflictError(PasswordAuthError):
    pass


class OAuthIdentityAlreadyLinkedError(PasswordAuthError):
    pass


def validate_username(value: str) -> str:
    normalized = (value or "").strip().lower().removeprefix("@")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise UsernameValidationError(
            "請輸入 4–20 個字元，只能使用小寫英文字母、數字、句點與底線"
        )
    if normalized.startswith(".") or normalized.endswith("."):
        raise UsernameValidationError("會員帳號不能以句點開頭或結尾")
    if ".." in normalized:
        raise UsernameValidationError("會員帳號不能包含連續句點")
    return normalized


def validate_password(value: str) -> str:
    password = value or ""
    if not 8 <= len(password) <= 72:
        raise PasswordValidationError("會員密碼須為 8–72 個字元")
    if not any(character.isalpha() for character in password):
        raise PasswordValidationError("會員密碼至少需要一個英文字母")
    if not any(character.isdigit() for character in password):
        raise PasswordValidationError("會員密碼至少需要一個數字")
    return password


def ensure_username_change_allowed(
    last_changed_at: datetime | None,
    *,
    now: datetime | None = None,
) -> None:
    if last_changed_at is None:
        return
    current = now or datetime.now(timezone.utc)
    if last_changed_at.tzinfo is None:
        last_changed_at = last_changed_at.replace(tzinfo=timezone.utc)
    available_at = last_changed_at + USERNAME_CHANGE_COOLDOWN
    if current < available_at:
        raise UsernameChangeCooldownError(
            "會員帳號每 7 天只能修改一次，下次可修改時間："
            + available_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        )


def ensure_oauth_unlink_allowed(
    *,
    providers: Iterable[str],
    has_password: bool,
    provider: str,
) -> None:
    normalized = (provider or "").strip().lower()
    if normalized not in SUPPORTED_OAUTH_PROVIDERS:
        raise PasswordAuthError("不支援的快速登入來源")
    remaining = [item for item in providers if item != normalized]
    if not has_password and not remaining:
        raise LastLoginMethodError(
            "這是目前唯一的登入方式，請先設定會員密碼或綁定其他登入方式再解除"
        )


def create_password_auth_service() -> Any:
    backend = os.getenv("AUTH_STORAGE_BACKEND", "postgres").strip().lower()
    if backend != "postgres":
        _LOGGER.warning(
            "AUTH_STORAGE_BACKEND=%s is no longer supported; using PostgreSQL", backend
        )
    from tools.auth.postgres_password_login import PostgresPasswordAuthService

    return PostgresPasswordAuthService()
