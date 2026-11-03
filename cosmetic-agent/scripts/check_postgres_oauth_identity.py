"""Verify new OAuth identity resolution with disposable development records."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.postgres import postgres_connection
from database.postgres.repositories.oauth_identity import (
    PostgresOAuthIdentityRepository,
)
from database.repositories.oauth_identity import OAuthIdentityInput


def main() -> None:
    suffix = uuid4().hex[:10]
    email = f"oauth_{suffix}@example.test"
    repository = PostgresOAuthIdentityRepository()
    created_user_ids: set[UUID] = set()

    try:
        google = repository.resolve_login(
            OAuthIdentityInput(
                provider="google",
                provider_subject=f"google-{suffix}",
                email=email,
                email_verified=True,
                display_name="OAuth Check",
            )
        )
        created_user_ids.add(google.user_id)

        line = repository.resolve_login(
            OAuthIdentityInput(
                provider="line",
                provider_subject=f"line-{suffix}",
                email=email.upper(),
                email_verified=True,
                display_name="OAuth Check",
            )
        )
        created_user_ids.add(line.user_id)

        google_again = repository.resolve_login(
            OAuthIdentityInput(
                provider="google",
                provider_subject=f"google-{suffix}",
                email=email,
                email_verified=True,
            )
        )

        unverified = repository.resolve_login(
            OAuthIdentityInput(
                provider="line",
                provider_subject=f"line-unverified-{suffix}",
                email=email,
                email_verified=False,
            )
        )
        created_user_ids.add(unverified.user_id)

        same_verified_account = google.user_id == line.user_id == google_again.user_id
        unverified_not_merged = unverified.user_id != google.user_id
        if not same_verified_account or not unverified_not_merged:
            raise RuntimeError("OAuth Email 合併規則驗證失敗")

        print(
            json.dumps(
                {
                    "google_new_user": google.matched_by == "new_user",
                    "line_matched_verified_email": line.matched_by == "verified_email",
                    "repeat_login_matched_provider": (
                        google_again.matched_by == "provider_identity"
                    ),
                    "same_verified_email_same_user": same_verified_account,
                    "unverified_email_not_merged": unverified_not_merged,
                    "unverified_account_status": unverified.account_status,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        if created_user_ids:
            with postgres_connection() as connection:
                connection.execute(
                    "DELETE FROM users WHERE id = ANY(%s)",
                    (list(created_user_ids),),
                )


if __name__ == "__main__":
    main()
