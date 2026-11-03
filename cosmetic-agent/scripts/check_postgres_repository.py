"""Exercise PostgreSQL identity reads with disposable development data."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.postgres import postgres_connection
from database.postgres.repositories import PostgresIdentityRepository
from database.storage_settings import identity_read_store


def main() -> None:
    suffix = uuid4().hex[:8]
    username = f"repo_{suffix}"
    email = f"repo_{suffix}@example.test"
    user_id = None

    try:
        with postgres_connection() as connection:
            user_id = connection.execute(
                "INSERT INTO users DEFAULT VALUES RETURNING id"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO usernames (user_id, username) VALUES (%s, %s)",
                (user_id, username),
            )
            connection.execute(
                """
                INSERT INTO user_emails
                    (user_id, email, is_primary, verified_at)
                VALUES (%s, %s, true, now())
                """,
                (user_id, email),
            )

        repository = PostgresIdentityRepository()
        by_id = repository.get_by_user_id(user_id)
        by_email = repository.get_by_email(email.upper())
        duplicate_available = repository.is_username_available(username.upper())
        unused_available = repository.is_username_available(f"free_{suffix}")

        passed = bool(
            by_id
            and by_email
            and by_id.user_id == user_id
            and by_email.user_id == user_id
            and by_id.email_verified
            and not duplicate_available
            and unused_available
        )
        if not passed:
            raise RuntimeError("PostgreSQL repository 驗證失敗")

        with postgres_connection() as connection:
            connection.execute("DELETE FROM users WHERE id = %s", (user_id,))
        user_id = None

        print(
            json.dumps(
                {
                    "configured_read_store": identity_read_store(),
                    "default_store_unchanged": identity_read_store() == "neo4j",
                    "email_lookup": "ok",
                    "user_id_lookup": "ok",
                    "username_availability": "ok",
                    "disposable_data_removed": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        if user_id is not None:
            with postgres_connection() as connection:
                connection.execute("DELETE FROM users WHERE id = %s", (user_id,))


if __name__ == "__main__":
    main()
