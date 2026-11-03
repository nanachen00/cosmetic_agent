"""Read-only audit for personal-data storage boundaries."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED = (
    "database/postgres/repositories/password_auth.py",
    "tools/auth/postgres_auth_session.py",
    "database/postgres/repositories/profile.py",
    "database/postgres/repositories/checkin.py",
    "database/postgres/repositories/quiz.py",
    "database/postgres/repositories/skin_record.py",
    "database/postgres/repositories/conversation.py",
)


def main() -> None:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    profile_source = (ROOT / "database/profile_repository.py").read_text()
    api_source = (ROOT / "api/api_server.py").read_text()

    checks = {
        "postgres_repositories": not missing,
        "neo4j_credentials_are_env_only": (
            'os.environ["NEO4J_URI"]' in profile_source
            and 'os.environ["NEO4J_USER"]' in profile_source
            and 'os.environ["NEO4J_PASSWORD"]' in profile_source
            and "databases.neo4j.io" not in profile_source
        ),
        "chat_uses_authenticated_uuid": "_chat_user_id(request" in api_source,
        "profile_has_postgres_path": "postgres_profile_repository" in api_source,
        "conversation_has_postgres_mapping": "postgres_conversation_repository" in api_source,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise SystemExit("DATA_BOUNDARY_AUDIT_FAILED: " + ", ".join(failed + missing))

    print("DATA_BOUNDARY_AUDIT_OK")
    for name in checks:
        print(f"{name}=ok")


if __name__ == "__main__":
    main()
