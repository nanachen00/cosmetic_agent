"""Small, explicit PostgreSQL connection boundary.

Existing Neo4j-backed services are not switched by importing this module.
Callers must opt in by using ``postgres_connection``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from typing import Any



class PostgresConfigurationError(RuntimeError):
    """Raised when the relational database is not configured."""


def _database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise PostgresConfigurationError("DATABASE_URL 尚未設定")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise PostgresConfigurationError("DATABASE_URL 必須是 PostgreSQL 連線字串")
    return database_url


@contextmanager
def postgres_connection(*, autocommit: bool = False) -> Iterator[Any]:
    """Yield one transaction-scoped connection and always close it."""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise RuntimeError("PostgreSQL access requires the optional psycopg package") from exc

    with psycopg.connect(
        _database_url(),
        connect_timeout=5,
        autocommit=autocommit,
        row_factory=dict_row,
    ) as connection:
        yield connection


def check_postgres_connection() -> dict[str, Any]:
    """Return non-sensitive connectivity metadata for diagnostics."""

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    current_user AS database_user,
                    current_database() AS database_name,
                    (
                        SELECT COUNT(*)
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                    ) AS public_tables
                """
            )
            result = cursor.fetchone()
    return dict(result)


def postgres_jsonb(value: Any) -> Any:
    """Wrap a value for PostgreSQL JSONB only when database access is used."""
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError as exc:
        raise RuntimeError("PostgreSQL access requires the optional psycopg package") from exc
    return Jsonb(value)
