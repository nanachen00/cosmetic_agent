"""PostgreSQL-backed browser sessions with the legacy service interface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_connection
from tools.auth.auth_session import (
    AuthSessionError,
    InvalidSessionError,
    RECENT_AUTH_SECONDS,
    RecentAuthenticationRequiredError,
    SESSION_TTL_SECONDS,
    create_session_token,
    hash_session_token,
)


class PostgresAuthSessionService:
    def create(
        self,
        *,
        user_id: str,
        auth_method: str,
        client_ip: str = "",
        user_agent: str = "",
        now: datetime | None = None,
    ) -> str:
        if not user_id or not auth_method:
            raise AuthSessionError("無法建立登入狀態")
        issued_at = now or datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(seconds=SESSION_TTL_SECONDS)
        token = create_session_token()
        try:
            canonical_user_id = UUID(str(user_id))
        except (TypeError, ValueError) as error:
            raise AuthSessionError("找不到會員資料，無法建立登入狀態") from error
        with postgres_connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id = %s AND deleted_at IS NULL AND status = 'active'",
                (canonical_user_id,),
            ).fetchone():
                raise AuthSessionError("找不到會員資料，無法建立登入狀態")
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    user_id, token_hash, auth_method, user_agent, ip_address,
                    created_at, expires_at, last_seen_at
                ) VALUES (%s, %s, %s, %s, NULLIF(%s, '')::inet, %s, %s, %s)
                """,
                (
                    canonical_user_id,
                    hash_session_token(token),
                    auth_method,
                    (user_agent or "")[:512],
                    (client_ip or "")[:128],
                    issued_at,
                    expires_at,
                    issued_at,
                ),
            )
        return token

    def authenticate(
        self,
        token: str,
        *,
        require_recent: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not token:
            raise InvalidSessionError("請先登入")
        current_time = now or datetime.now(timezone.utc)
        with postgres_connection() as connection:
            row = connection.execute(
                """
                UPDATE auth_sessions AS session
                SET last_seen_at = %s
                FROM users AS users
                LEFT JOIN usernames AS names ON names.user_id = users.id
                WHERE session.token_hash = %s
                  AND session.user_id = users.id
                  AND session.revoked_at IS NULL
                  AND session.expires_at > %s
                  AND users.deleted_at IS NULL
                    AND users.status = 'active'
                RETURNING users.id AS user_id, names.username,
                          session.auth_method,
                          session.created_at AS authenticated_at
                """,
                (current_time, hash_session_token(token), current_time),
            ).fetchone()
        if row is None:
            raise InvalidSessionError("登入狀態已失效，請重新登入")
        is_recent = row["authenticated_at"] >= current_time - timedelta(
            seconds=RECENT_AUTH_SECONDS
        )
        if require_recent and not is_recent:
            raise RecentAuthenticationRequiredError("請重新登入後再執行此操作")
        return {
            "user_id": str(row["user_id"]),
            "username": row["username"],
            "auth_method": row["auth_method"],
            "authenticated_at": str(row["authenticated_at"]),
        }

    def revoke(self, token: str) -> None:
        if not token:
            return
        with postgres_connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = now() "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (hash_session_token(token),),
            )

    def revoke_all_for_user(self, user_id: str) -> None:
        try:
            canonical_user_id = UUID(str(user_id))
        except (TypeError, ValueError):
            return
        with postgres_connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = now() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (canonical_user_id,),
            )
