"""Disposable integration check for PostgreSQL password reset challenges."""

from __future__ import annotations

import json
import secrets

from database.postgres.connection import postgres_connection
from database.postgres.repositories.password_auth import (
    PostgresPasswordAuthRepository,
)
from database.postgres.repositories.password_reset import (
    PostgresPasswordResetRepository,
)
from database.repositories.password_reset import (
    InvalidPasswordResetCodeError,
    PasswordResetRateLimitedError,
)


def main() -> None:
    suffix = secrets.token_hex(5)
    username = f"reset_{suffix}"[:20]
    email = f"reset_{suffix}@example.test"
    original_password = "BlinkReset123"
    new_password = "BlinkReset456"
    password_repository = PostgresPasswordAuthRepository()
    reset_repository = PostgresPasswordResetRepository(
        secret="integration-test-only-secret-32-characters-minimum",
        code_factory=lambda: "123456",
    )
    account = None
    results: dict[str, bool] = {}

    try:
        account = password_repository.register(
            username=username,
            verified_email=email,
            password=original_password,
        )
        dispatch = reset_repository.issue(email=email)
        results["fixed_test_code"] = dispatch.code == "123456"
        with postgres_connection() as connection:
            stored = connection.execute(
                "SELECT code_hash, attempt_count, consumed_at "
                "FROM password_reset_challenges WHERE id = %s",
                (dispatch.challenge_id,),
            ).fetchone()
        results["code_hash_only"] = (
            stored["code_hash"] != dispatch.code
            and len(stored["code_hash"]) == 64
            and dispatch.code not in stored["code_hash"]
        )

        try:
            reset_repository.consume(
                challenge_id=dispatch.challenge_id,
                code="000000" if dispatch.code != "000000" else "999999",
                new_password=new_password,
            )
        except InvalidPasswordResetCodeError:
            results["wrong_code_rejected"] = True
        else:
            results["wrong_code_rejected"] = False

        with postgres_connection() as connection:
            attempts = connection.execute(
                "SELECT attempt_count FROM password_reset_challenges WHERE id = %s",
                (dispatch.challenge_id,),
            ).fetchone()["attempt_count"]
        results["wrong_attempt_persisted"] = attempts == 1

        reset_user_id = reset_repository.consume(
            challenge_id=dispatch.challenge_id,
            code=dispatch.code,
            new_password=new_password,
        )
        results["new_password_login"] = (
            reset_user_id == account.user_id
            and password_repository.authenticate(
                username=username,
                password=new_password,
            ).user_id
            == account.user_id
        )

        try:
            reset_repository.consume(
                challenge_id=dispatch.challenge_id,
                code=dispatch.code,
                new_password=original_password,
            )
        except InvalidPasswordResetCodeError:
            results["code_single_use"] = True
        else:
            results["code_single_use"] = False

        reset_repository.issue(email=email)
        reset_repository.issue(email=email)
        try:
            reset_repository.issue(email=email)
        except PasswordResetRateLimitedError:
            results["send_rate_limited"] = True
        else:
            results["send_rate_limited"] = False

        if not all(results.values()):
            raise AssertionError(results)
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
    finally:
        if account is not None:
            with postgres_connection() as connection:
                connection.execute("DELETE FROM users WHERE id = %s", (account.user_id,))


if __name__ == "__main__":
    main()
