"""Read-only PostgreSQL account identity repository."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID


from database.postgres.connection import postgres_connection
from database.repositories.identity import AccountIdentity

ConnectionContextFactory = Callable[
    [], AbstractContextManager[Any]
]

_ACCOUNT_SELECT = """
    SELECT
        u.id AS user_id,
        u.status,
        un.username,
        email.email AS primary_email,
        (email.verified_at IS NOT NULL) AS email_verified,
        oauth.display_name,
        username_change.changed_at AS username_changed_at,
        COALESCE(
            array_agg(ai.provider ORDER BY ai.provider)
                FILTER (WHERE ai.provider IS NOT NULL),
            ARRAY[]::text[]
        ) AS providers,
        (pc.user_id IS NOT NULL) AS has_password
    FROM users AS u
    LEFT JOIN usernames AS un ON un.user_id = u.id
    LEFT JOIN LATERAL (
        SELECT ue.email, ue.verified_at
        FROM user_emails AS ue
        WHERE ue.user_id = u.id
        ORDER BY ue.is_primary DESC, ue.created_at ASC
        LIMIT 1
    ) AS email ON true
    LEFT JOIN LATERAL (
        SELECT identity.display_name
        FROM auth_identities AS identity
        WHERE identity.user_id = u.id
          AND identity.display_name IS NOT NULL
          AND trim(identity.display_name) <> ''
        ORDER BY identity.last_login_at DESC NULLS LAST,
                 identity.linked_at DESC
        LIMIT 1
    ) AS oauth ON true
    LEFT JOIN LATERAL (
        SELECT history.changed_at
        FROM username_history AS history
        WHERE history.user_id = u.id
        ORDER BY history.changed_at DESC
        LIMIT 1
    ) AS username_change ON true
    LEFT JOIN auth_identities AS ai ON ai.user_id = u.id
    LEFT JOIN password_credentials AS pc ON pc.user_id = u.id
"""

_ACCOUNT_GROUP_BY = """
    GROUP BY
        u.id,
        u.status,
        un.username,
        email.email,
        email.verified_at,
        oauth.display_name,
        username_change.changed_at,
        pc.user_id
    LIMIT 1
"""


class PostgresIdentityRepository:
    """Fetch canonical account identity without changing current auth behavior."""

    def __init__(
        self,
        connection_factory: ConnectionContextFactory = postgres_connection,
    ) -> None:
        self._connection_factory = connection_factory

    @staticmethod
    def _from_row(row: dict[str, Any] | None) -> AccountIdentity | None:
        if row is None:
            return None
        return AccountIdentity(
            user_id=row["user_id"],
            status=row["status"],
            username=row["username"],
            primary_email=row["primary_email"],
            email_verified=bool(row["email_verified"]),
            providers=tuple(row["providers"]),
            has_password=bool(row["has_password"]),
            display_name=row["display_name"],
            username_changed_at=row["username_changed_at"],
        )

    def _fetch_one(
        self,
        where_clause: str,
        parameters: tuple[object, ...],
    ) -> AccountIdentity | None:
        query = f"{_ACCOUNT_SELECT} WHERE {where_clause} {_ACCOUNT_GROUP_BY}"
        with self._connection_factory() as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._from_row(row)

    def get_by_user_id(self, user_id: UUID | str) -> AccountIdentity | None:
        return self._fetch_one("u.id = %s", (user_id,))

    def get_by_email(self, email: str) -> AccountIdentity | None:
        normalized_email = email.strip().lower()
        if not normalized_email:
            return None
        return self._fetch_one(
            "EXISTS ("
            "SELECT 1 FROM user_emails AS lookup_email "
            "WHERE lookup_email.user_id = u.id "
            "AND lower(lookup_email.email) = %s"
            ")",
            (normalized_email,),
        )

    def is_username_available(self, username: str) -> bool:
        normalized_username = username.strip().lower()
        if not normalized_username:
            return False
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT NOT EXISTS ("
                "SELECT 1 FROM usernames WHERE lower(username) = %s"
                ") AS available",
                (normalized_username,),
            ).fetchone()
        return bool(row and row["available"])
