"""Atomic PostgreSQL resolution for new Google and LINE identities."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_connection
from database.repositories.oauth_identity import (
    OAuthIdentityConflictError,
    OAuthIdentityInput,
    OAuthIdentityResolution,
    OAuthIdentityValidationError,
)

ConnectionContextFactory = Callable[
    [], AbstractContextManager[Any]
]

_ALLOWED_PROVIDERS = {"google", "line"}
_USERNAME_CHARACTERS = re.compile(r"[^a-z0-9._]+")
_RESERVED_USERNAMES = {
    "admin",
    "administrator",
    "blink",
    "blinkai",
    "help",
    "moderator",
    "official",
    "root",
    "support",
    "system",
}


def _normalized_email(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    return normalized or None


def _username_base(identity: OAuthIdentityInput) -> str:
    email_prefix = (_normalized_email(identity.email) or "").partition("@")[0]
    raw = email_prefix or (identity.display_name or "")
    ascii_value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    value = _USERNAME_CHARACTERS.sub("", ascii_value.strip().lower())
    value = value.strip(".")
    while ".." in value:
        value = value.replace("..", ".")
    if len(value) < 4 or value in _RESERVED_USERNAMES:
        return "blink_user"
    return value[:20]


class PostgresOAuthIdentityRepository:
    """Resolve OAuth identities while treating only verified email as mergeable."""

    def __init__(
        self,
        connection_factory: ConnectionContextFactory = postgres_connection,
    ) -> None:
        self._connection_factory = connection_factory

    @staticmethod
    def _validate(identity: OAuthIdentityInput) -> tuple[str, str, str | None]:
        provider = identity.provider.strip().lower()
        subject = identity.provider_subject.strip()
        email = _normalized_email(identity.email)
        if provider not in _ALLOWED_PROVIDERS:
            raise OAuthIdentityValidationError("不支援的 OAuth 登入供應商")
        if not subject:
            raise OAuthIdentityValidationError("OAuth 身分缺少 provider subject")
        if identity.email_verified and not email:
            raise OAuthIdentityValidationError("已驗證的 OAuth 身分必須包含 Email")
        return provider, subject, email

    @staticmethod
    def _lock_identity_keys(
        connection: Any,
        *,
        provider: str,
        subject: str,
        verified_email: str | None,
    ) -> None:
        keys = [f"oauth:{provider}:{subject}"]
        if verified_email:
            keys.append(f"email:{verified_email}")
        for key in sorted(keys):
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (key,),
            )

    @staticmethod
    def _username_for_new_user(
        connection: Any,
        *,
        user_id: UUID,
        identity: OAuthIdentityInput,
    ) -> str:
        base = _username_base(identity)
        available = connection.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM usernames WHERE lower(username) = %s) "
            "AS available",
            (base,),
        ).fetchone()["available"]
        username = base if available else f"{base[:11]}_{user_id.hex[:8]}"
        connection.execute(
            "INSERT INTO usernames (user_id, username) VALUES (%s, %s)",
            (user_id, username),
        )
        return username

    @staticmethod
    def _upsert_verified_email(
        connection: Any,
        *,
        user_id: UUID,
        email: str,
    ) -> None:
        owner = connection.execute(
            "SELECT user_id FROM user_emails WHERE lower(email) = %s FOR UPDATE",
            (email,),
        ).fetchone()
        if owner and owner["user_id"] != user_id:
            raise OAuthIdentityConflictError("此已驗證 Email 屬於另一個會員")
        if owner:
            connection.execute(
                "UPDATE user_emails SET verified_at = COALESCE(verified_at, now()) "
                "WHERE user_id = %s AND lower(email) = %s",
                (user_id, email),
            )
            return

        has_primary = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM user_emails "
            "WHERE user_id = %s AND is_primary) AS has_primary",
            (user_id,),
        ).fetchone()["has_primary"]
        connection.execute(
            """
            INSERT INTO user_emails (user_id, email, is_primary, verified_at)
            VALUES (%s, %s, %s, now())
            """,
            (user_id, email, not has_primary),
        )

    def resolve_login(self, identity: OAuthIdentityInput) -> OAuthIdentityResolution:
        provider, subject, email = self._validate(identity)
        verified_email = email if identity.email_verified else None

        with self._connection_factory() as connection:
            self._lock_identity_keys(
                connection,
                provider=provider,
                subject=subject,
                verified_email=verified_email,
            )

            existing_identity = connection.execute(
                """
                SELECT ai.user_id, u.status, un.username
                FROM auth_identities AS ai
                JOIN users AS u ON u.id = ai.user_id
                JOIN usernames AS un ON un.user_id = u.id
                WHERE ai.provider = %s AND ai.provider_subject = %s
                FOR UPDATE OF ai, u, un
                """,
                (provider, subject),
            ).fetchone()
            if existing_identity:
                user_id = existing_identity["user_id"]
                status = existing_identity["status"]
                if verified_email:
                    email_owner = connection.execute(
                        "SELECT user_id FROM user_emails "
                        "WHERE lower(email) = %s FOR UPDATE",
                        (verified_email,),
                    ).fetchone()

                    # The provider subject is the authoritative identity for an
                    # existing OAuth login.  A conflicting provider email must
                    # never move or merge accounts, but it must not lock the
                    # legitimate LINE/Google identity out of its own account.
                    if not email_owner or email_owner["user_id"] == user_id:
                        self._upsert_verified_email(
                            connection,
                            user_id=user_id,
                            email=verified_email,
                        )
                if status == "pending":
                    connection.execute(
                        "UPDATE users SET status = 'active', updated_at = now() "
                        "WHERE id = %s",
                        (user_id,),
                    )
                    status = "active"
                connection.execute(
                    """
                    UPDATE auth_identities
                    SET provider_email = COALESCE(%s, provider_email),
                        provider_email_verified = provider_email_verified OR %s,
                        display_name = COALESCE(%s, display_name),
                        avatar_url = COALESCE(%s, avatar_url),
                        last_login_at = now()
                    WHERE provider = %s AND provider_subject = %s
                    """,
                    (
                        email,
                        identity.email_verified,
                        identity.display_name,
                        identity.avatar_url,
                        provider,
                        subject,
                    ),
                )
                return OAuthIdentityResolution(
                    user_id=user_id,
                    username=existing_identity["username"],
                    account_status=status,
                    created_user=False,
                    linked_identity=False,
                    matched_by="provider_identity",
                )

            email_owner = None
            if verified_email:
                email_owner = connection.execute(
                    "SELECT user_id FROM user_emails "
                    "WHERE lower(email) = %s FOR UPDATE",
                    (verified_email,),
                ).fetchone()

            created_user = email_owner is None
            if created_user:
                # A verified LINE/Google identity is sufficient for an active
                # account. Email is optional for LINE Login.
                status = "active"
                user_id = connection.execute(
                    "INSERT INTO users (status) VALUES (%s) RETURNING id",
                    (status,),
                ).fetchone()["id"]
                username = self._username_for_new_user(
                    connection,
                    user_id=user_id,
                    identity=identity,
                )
                if verified_email:
                    self._upsert_verified_email(
                        connection,
                        user_id=user_id,
                        email=verified_email,
                    )
                matched_by = "new_user"
            else:
                user_id = email_owner["user_id"]
                account = connection.execute(
                    """
                    SELECT u.status, un.username
                    FROM users AS u
                    JOIN usernames AS un ON un.user_id = u.id
                    WHERE u.id = %s
                    FOR UPDATE OF u, un
                    """,
                    (user_id,),
                ).fetchone()
                if account is None:
                    raise OAuthIdentityConflictError("Email 所屬會員資料不完整")
                existing_provider = connection.execute(
                    "SELECT provider_subject FROM auth_identities "
                    "WHERE user_id = %s AND provider = %s FOR UPDATE",
                    (user_id, provider),
                ).fetchone()
                if existing_provider:
                    raise OAuthIdentityConflictError(
                        "此會員已綁定另一個同類型 OAuth 身分"
                    )
                connection.execute(
                    "UPDATE users SET status = 'active', updated_at = now() "
                    "WHERE id = %s AND status = 'pending'",
                    (user_id,),
                )
                username = account["username"]
                status = "active"
                matched_by = "verified_email"

            connection.execute(
                """
                INSERT INTO auth_identities (
                    user_id,
                    provider,
                    provider_subject,
                    provider_email,
                    provider_email_verified,
                    display_name,
                    avatar_url,
                    last_login_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    user_id,
                    provider,
                    subject,
                    email,
                    identity.email_verified,
                    identity.display_name,
                    identity.avatar_url,
                ),
            )

            return OAuthIdentityResolution(
                user_id=user_id,
                username=username,
                account_status=status,
                created_user=created_user,
                linked_identity=True,
                matched_by=matched_by,
            )
