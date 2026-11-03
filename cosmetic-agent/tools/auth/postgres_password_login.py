"""Compatibility service that keeps the legacy auth API contract on PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from argon2 import PasswordHasher

from database.postgres.connection import postgres_connection
from database.postgres.repositories.identity import PostgresIdentityRepository
from database.postgres.repositories.oauth_identity import PostgresOAuthIdentityRepository
from database.postgres.repositories.password_auth import PostgresPasswordAuthRepository
from database.repositories.oauth_identity import (
    OAuthIdentityConflictError,
    OAuthIdentityInput,
)
from database.repositories.password_auth import (
    AccountLockedError as RepositoryAccountLockedError,
    InvalidCredentialsError as RepositoryInvalidCredentialsError,
    PasswordAlreadySetError as RepositoryPasswordAlreadySetError,
    PasswordAuthValidationError,
    PasswordNotSetError as RepositoryPasswordNotSetError,
    UsernameUnavailableError,
)
from tools.auth.password_login import (
    AccountNotFoundError,
    EmailIdentityConflictError,
    ensure_oauth_unlink_allowed,
    InvalidCredentialsError,
    LoginRateLimitedError,
    OAuthIdentityAlreadyLinkedError,
    PasswordAlreadySetError,
    PasswordAuthError,
    PasswordNotSetError,
    PasswordValidationError,
    UsernameAlreadyExistsError,
    UsernameAlreadySetError,
    UsernameValidationError,
    ensure_username_change_allowed,
    validate_password,
    validate_username,
)


class PostgresPasswordAuthService:
    """Expose the old service methods while PostgreSQL owns account identity."""

    def __init__(self) -> None:
        self.identity_repository = PostgresIdentityRepository()
        self.oauth_repository = PostgresOAuthIdentityRepository()
        self.password_repository = PostgresPasswordAuthRepository()
        self._hasher = PasswordHasher()

    @staticmethod
    def _uuid(user_id: str) -> UUID:
        try:
            return UUID(str(user_id))
        except (TypeError, ValueError) as error:
            raise AccountNotFoundError("找不到會員資料，請重新登入") from error

    @staticmethod
    def _account_dict(account: Any) -> dict[str, Any]:
        username_changed_at = account.username_changed_at
        username_change_available_at = (
            username_changed_at + timedelta(days=7)
            if username_changed_at is not None
            else None
        )
        return {
            "user_id": str(account.user_id),
            "username": account.username,
            "display_name": account.display_name,
            "email": account.primary_email,
            "email_verified": account.email_verified,
            "password_set": account.has_password,
            "providers": list(account.providers),
            "username_changed_at": (
                username_changed_at.isoformat()
                if username_changed_at is not None
                else None
            ),
            "username_change_available_at": (
                username_change_available_at.isoformat()
                if username_change_available_at is not None
                else None
            ),
        }

    def is_username_available(self, username: str) -> bool:
        try:
            normalized = validate_username(username)
        except Exception as error:
            raise UsernameValidationError(str(error)) from error
        return self.identity_repository.is_username_available(normalized)

    def get_user_account(self, user_id: str) -> dict[str, Any] | None:
        try:
            account = self.identity_repository.get_by_user_id(self._uuid(user_id))
        except AccountNotFoundError:
            return None
        return self._account_dict(account) if account else None

    def ensure_oauth_user(
        self,
        *,
        user_id: str,
        provider: str,
        provider_subject: str,
        display_name: str = "",
        email: str = "",
        email_verified: bool = False,
    ) -> dict[str, Any]:
        del user_id  # PostgreSQL generates the canonical UUID.
        try:
            result = self.oauth_repository.resolve_login(
                OAuthIdentityInput(
                    provider=provider,
                    provider_subject=provider_subject,
                    display_name=display_name or None,
                    email=email or None,
                    email_verified=email_verified,
                )
            )
        except OAuthIdentityConflictError as error:
            raise EmailIdentityConflictError(str(error)) from error
        account = self.get_user_account(str(result.user_id))
        if account is None:
            raise AccountNotFoundError("快速登入會員建立失敗")
        account["display_name"] = display_name or account["display_name"]
        return account

    def link_oauth_identity(
        self,
        *,
        user_id: str,
        provider: str,
        provider_subject: str,
        display_name: str = "",
        email: str = "",
        email_verified: bool = False,
    ) -> dict[str, Any]:
        canonical_user_id = self._uuid(user_id)
        normalized_email = (email or "").strip().lower() or None
        if provider not in {"google", "line"} or not provider_subject:
            raise PasswordAuthError("快速登入資料不完整")
        try:
            with postgres_connection() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"oauth:{provider}:{provider_subject}",),
                )
                if not connection.execute(
                    "SELECT 1 FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                    (canonical_user_id,),
                ).fetchone():
                    raise AccountNotFoundError("找不到目前登入的會員")
                owner = connection.execute(
                    "SELECT user_id FROM auth_identities "
                    "WHERE provider = %s AND provider_subject = %s FOR UPDATE",
                    (provider, provider_subject),
                ).fetchone()
                if owner and owner["user_id"] != canonical_user_id:
                    raise OAuthIdentityAlreadyLinkedError(
                        f"此 {provider.upper()} 帳號已綁定其他會員"
                    )
                provider_owner = connection.execute(
                    "SELECT provider_subject FROM auth_identities "
                    "WHERE user_id = %s AND provider = %s FOR UPDATE",
                    (canonical_user_id, provider),
                ).fetchone()
                if provider_owner and provider_owner["provider_subject"] != provider_subject:
                    raise OAuthIdentityAlreadyLinkedError(
                        f"此會員已綁定另一個 {provider.upper()} 帳號"
                    )
                if normalized_email and email_verified:
                    email_owner = connection.execute(
                        "SELECT user_id FROM user_emails WHERE lower(email) = %s FOR UPDATE",
                        (normalized_email,),
                    ).fetchone()
                    if email_owner and email_owner["user_id"] != canonical_user_id:
                        raise EmailIdentityConflictError(
                            "此 Email 已屬於其他會員，請改以該會員登入"
                        )
                    if not email_owner:
                        has_primary = connection.execute(
                            "SELECT EXISTS (SELECT 1 FROM user_emails "
                            "WHERE user_id = %s AND is_primary) AS value",
                            (canonical_user_id,),
                        ).fetchone()["value"]
                        connection.execute(
                            "INSERT INTO user_emails "
                            "(user_id, email, is_primary, verified_at) "
                            "VALUES (%s, %s, %s, now())",
                            (canonical_user_id, normalized_email, not has_primary),
                        )
                connection.execute(
                    """
                    INSERT INTO auth_identities (
                        user_id, provider, provider_subject, provider_email,
                        provider_email_verified, display_name, last_login_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (provider, provider_subject) DO UPDATE SET
                        provider_email = COALESCE(EXCLUDED.provider_email,
                                                  auth_identities.provider_email),
                        provider_email_verified =
                            auth_identities.provider_email_verified
                            OR EXCLUDED.provider_email_verified,
                        display_name = COALESCE(EXCLUDED.display_name,
                                                auth_identities.display_name),
                        last_login_at = now()
                    """,
                    (
                        canonical_user_id,
                        provider,
                        provider_subject,
                        normalized_email,
                        email_verified,
                        display_name or None,
                    ),
                )
        except psycopg.errors.UniqueViolation as error:
            raise OAuthIdentityAlreadyLinkedError(
                f"此會員已綁定另一個 {provider.upper()} 帳號"
            ) from error
        account = self.get_user_account(str(canonical_user_id))
        if account is None:
            raise AccountNotFoundError("找不到目前登入的會員")
        return account

    def unlink_oauth_identity(
        self,
        *,
        user_id: str,
        provider: str,
    ) -> dict[str, Any]:
        canonical_user_id = self._uuid(user_id)
        normalized_provider = (provider or "").strip().lower()

        with postgres_connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"oauth-unlink:{canonical_user_id}",),
            )
            if not connection.execute(
                "SELECT 1 FROM users "
                "WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (canonical_user_id,),
            ).fetchone():
                raise AccountNotFoundError("找不到目前登入的會員")

            identity_rows = connection.execute(
                "SELECT provider FROM auth_identities "
                "WHERE user_id = %s FOR UPDATE",
                (canonical_user_id,),
            ).fetchall()
            has_password = bool(
                connection.execute(
                    "SELECT 1 FROM password_credentials WHERE user_id = %s",
                    (canonical_user_id,),
                ).fetchone()
            )
            providers = [row["provider"] for row in identity_rows]
            ensure_oauth_unlink_allowed(
                providers=providers,
                has_password=has_password,
                provider=normalized_provider,
            )
            if normalized_provider in providers:
                connection.execute(
                    "DELETE FROM auth_identities "
                    "WHERE user_id = %s AND provider = %s",
                    (canonical_user_id, normalized_provider),
                )

        account = self.get_user_account(str(canonical_user_id))
        if account is None:
            raise AccountNotFoundError("找不到目前登入的會員")
        return account

    def claim_username(self, *, user_id: str, username: str) -> dict[str, str]:
        normalized = validate_username(username)
        canonical_user_id = self._uuid(user_id)
        try:
            with postgres_connection() as connection:
                current = connection.execute(
                    "SELECT username FROM usernames WHERE user_id = %s FOR UPDATE",
                    (canonical_user_id,),
                ).fetchone()
                if current and current["username"] != normalized:
                    raise UsernameAlreadySetError("會員帳號已設定，不能再次變更")
                if not current:
                    connection.execute(
                        "INSERT INTO usernames (user_id, username) VALUES (%s, %s)",
                        (canonical_user_id, normalized),
                    )
        except psycopg.errors.UniqueViolation as error:
            raise UsernameAlreadyExistsError("這個會員帳號已被使用") from error
        return {"user_id": str(canonical_user_id), "username": normalized}

    def change_username(self, *, user_id: str, username: str) -> dict[str, Any]:
        normalized = validate_username(username)
        canonical_user_id = self._uuid(user_id)
        now = datetime.now(timezone.utc)

        try:
            with postgres_connection() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"username-change:{canonical_user_id}",),
                )
                current = connection.execute(
                    "SELECT username FROM usernames "
                    "WHERE user_id = %s FOR UPDATE",
                    (canonical_user_id,),
                ).fetchone()
                if current is None:
                    raise AccountNotFoundError("找不到會員帳號")
                if current["username"].lower() == normalized:
                    return self.get_user_account(str(canonical_user_id)) or {
                        "user_id": str(canonical_user_id),
                        "username": normalized,
                    }

                history = connection.execute(
                    "SELECT changed_at FROM username_history "
                    "WHERE user_id = %s ORDER BY changed_at DESC LIMIT 1",
                    (canonical_user_id,),
                ).fetchone()
                ensure_username_change_allowed(
                    history["changed_at"] if history else None,
                    now=now,
                )

                owner = connection.execute(
                    "SELECT user_id FROM usernames "
                    "WHERE lower(username) = %s AND user_id <> %s",
                    (normalized, canonical_user_id),
                ).fetchone()
                if owner is not None:
                    raise UsernameAlreadyExistsError("這個會員帳號已被使用")

                connection.execute(
                    "INSERT INTO username_history "
                    "(user_id, old_username, changed_at) VALUES (%s, %s, %s)",
                    (canonical_user_id, current["username"], now),
                )
                connection.execute(
                    "UPDATE usernames SET username = %s, updated_at = %s "
                    "WHERE user_id = %s",
                    (normalized, now, canonical_user_id),
                )
        except psycopg.errors.UniqueViolation as error:
            raise UsernameAlreadyExistsError("這個會員帳號已被使用") from error

        account = self.get_user_account(str(canonical_user_id))
        if account is None:
            raise AccountNotFoundError("找不到會員帳號")
        return account

    def register(self, *, username: str, password: str) -> dict[str, str]:
        del username, password
        raise PasswordValidationError("請先完成 Email 驗證再建立會員帳號")

    def login(
        self, *, username: str, password: str, client_key: str = "unknown"
    ) -> dict[str, str]:
        del client_key
        try:
            account = self.password_repository.authenticate(
                username=username, password=password
            )
        except RepositoryAccountLockedError as error:
            raise LoginRateLimitedError(str(error)) from error
        except (RepositoryInvalidCredentialsError, RepositoryPasswordNotSetError) as error:
            raise InvalidCredentialsError("會員帳號或密碼不正確") from error
        except PasswordAuthValidationError as error:
            raise InvalidCredentialsError("會員帳號或密碼不正確") from error
        return {"user_id": str(account.user_id), "username": account.username}

    def set_initial_password(self, *, user_id: str, password: str) -> dict[str, str]:
        try:
            self.password_repository.set_initial_password(
                user_id=self._uuid(user_id), new_password=password
            )
        except PasswordAuthValidationError as error:
            raise PasswordValidationError(str(error)) from error
        except RepositoryPasswordAlreadySetError as error:
            raise PasswordAlreadySetError(str(error)) from error
        account = self.get_user_account(user_id)
        if account is None:
            raise AccountNotFoundError("找不到會員資料，請重新登入")
        return {"user_id": user_id, "username": account["username"]}

    def change_password(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
        client_key: str = "unknown",
    ) -> dict[str, str]:
        del client_key
        try:
            self.password_repository.change_password(
                user_id=self._uuid(user_id),
                current_password=current_password,
                new_password=new_password,
            )
        except PasswordAuthValidationError as error:
            raise PasswordValidationError(str(error)) from error
        except RepositoryInvalidCredentialsError as error:
            raise InvalidCredentialsError(str(error)) from error
        except RepositoryAccountLockedError as error:
            raise LoginRateLimitedError(str(error)) from error
        except RepositoryPasswordNotSetError as error:
            raise PasswordNotSetError(str(error)) from error
        account = self.get_user_account(user_id)
        if account is None:
            raise AccountNotFoundError("找不到會員資料")
        return {"user_id": user_id, "username": account["username"]}

    def verify_current_password(
        self,
        *,
        user_id: str,
        current_password: str,
        client_key: str = "unknown",
    ) -> None:
        del client_key
        account = self.get_user_account(user_id)
        if account is None:
            raise AccountNotFoundError("找不到會員資料")
        if not account["password_set"]:
            raise PasswordNotSetError("尚未設定會員密碼")
        try:
            self.password_repository.authenticate(
                username=account["username"], password=current_password
            )
        except RepositoryAccountLockedError as error:
            raise LoginRateLimitedError(str(error)) from error
        except RepositoryInvalidCredentialsError as error:
            raise InvalidCredentialsError("原密碼不正確") from error

    def reset_password(self, *, user_id: str, new_password: str) -> dict[str, str]:
        validate_password(new_password)
        canonical_user_id = self._uuid(user_id)
        password_hash = self._hasher.hash(new_password)
        with postgres_connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (canonical_user_id,),
            ).fetchone():
                raise AccountNotFoundError("找不到會員資料")
            connection.execute(
                """
                INSERT INTO password_credentials
                    (user_id, password_hash, hash_algorithm)
                VALUES (%s, %s, 'argon2id')
                ON CONFLICT (user_id) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    hash_algorithm = 'argon2id',
                    password_changed_at = now(),
                    failed_attempts = 0,
                    locked_until = NULL
                """,
                (canonical_user_id, password_hash),
            )
        account = self.get_user_account(user_id)
        if account is None:
            raise AccountNotFoundError("找不到會員資料")
        return {"user_id": user_id, "username": account["username"]}
