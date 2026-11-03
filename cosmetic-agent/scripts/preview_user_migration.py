"""Create a privacy-safe, read-only preview of the user migration.

The report contains aggregate counts only. It never prints user ids, emails,
usernames, password hashes, OAuth subjects, or database credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from neo4j import GraphDatabase

from database.postgres import postgres_connection


class MigrationPreviewConfigurationError(RuntimeError):
    """Raised when a required source connection setting is missing."""


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MigrationPreviewConfigurationError(f"{name} 尚未設定")
    return value


def _safe_record(record: Any) -> dict[str, Any]:
    return {key: record[key] for key in record.keys()}


def _neo4j_preview() -> dict[str, Any]:
    driver = GraphDatabase.driver(
        _required_environment("NEO4J_URI"),
        auth=(
            _required_environment("NEO4J_USER"),
            _required_environment("NEO4J_PASSWORD"),
        ),
    )

    summary_query = """
        MATCH (u:User)
        WITH
            u,
            trim(coalesce(toString(u.user_id), '')) AS user_id,
            toLower(trim(coalesce(
                u.email_normalized,
                u.oauth_email,
                u.email,
                ''
            ))) AS email,
            toLower(trim(coalesce(
                u.username_normalized,
                u.username,
                ''
            ))) AS username
        RETURN
            count(u) AS users_total,
            sum(CASE WHEN user_id = '' THEN 1 ELSE 0 END) AS missing_user_id,
            sum(CASE WHEN user_id <> '' THEN 1 ELSE 0 END) AS with_user_id,
            sum(CASE
                WHEN user_id =~ '(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                THEN 1 ELSE 0
            END) AS uuid_compatible_user_id,
            sum(CASE WHEN email = '' THEN 1 ELSE 0 END) AS missing_email,
            sum(CASE WHEN email <> '' THEN 1 ELSE 0 END) AS with_email,
            sum(CASE WHEN username = '' THEN 1 ELSE 0 END) AS missing_username,
            sum(CASE WHEN username <> '' THEN 1 ELSE 0 END) AS with_username,
            sum(CASE WHEN coalesce(u.email_verified, false) THEN 1 ELSE 0 END)
                AS verified_email
    """

    duplicate_user_id_query = """
        MATCH (u:User)
        WITH trim(coalesce(toString(u.user_id), '')) AS value, count(*) AS members
        WHERE value <> '' AND members > 1
        RETURN count(*) AS duplicate_groups, coalesce(sum(members), 0) AS affected_users
    """

    duplicate_email_query = """
        MATCH (u:User)
        WITH toLower(trim(coalesce(
            u.email_normalized,
            u.oauth_email,
            u.email,
            ''
        ))) AS value, count(*) AS members
        WHERE value <> '' AND members > 1
        RETURN count(*) AS duplicate_groups, coalesce(sum(members), 0) AS affected_users
    """

    duplicate_username_query = """
        MATCH (u:User)
        WITH toLower(trim(coalesce(
            u.username_normalized,
            u.username,
            ''
        ))) AS value, count(*) AS members
        WHERE value <> '' AND members > 1
        RETURN count(*) AS duplicate_groups, coalesce(sum(members), 0) AS affected_users
    """

    relationship_query = """
        OPTIONAL MATCH (oauth:OAuthIdentity)
        WITH count(oauth) AS oauth_identity_nodes
        OPTIONAL MATCH (password:PasswordIdentity)
        WITH oauth_identity_nodes, count(password) AS password_identity_nodes
        OPTIONAL MATCH (orphan_oauth:OAuthIdentity)
        WHERE NOT EXISTS {
            MATCH (:User)-[:HAS_OAUTH_IDENTITY]->(orphan_oauth)
        }
        WITH oauth_identity_nodes, password_identity_nodes,
             count(orphan_oauth) AS orphan_oauth_identity_nodes
        OPTIONAL MATCH (orphan_password:PasswordIdentity)
        WHERE NOT EXISTS {
            MATCH (:User)-[:HAS_PASSWORD_IDENTITY]->(orphan_password)
        }
        RETURN
            oauth_identity_nodes,
            password_identity_nodes,
            orphan_oauth_identity_nodes,
            count(orphan_password) AS orphan_password_identity_nodes
    """

    provider_query = """
        MATCH (:User)-[:HAS_OAUTH_IDENTITY]->(identity:OAuthIdentity)
        WITH
            toLower(trim(coalesce(identity.provider, ''))) AS provider,
            count(*) AS identities
        ORDER BY provider
        RETURN collect({
            provider: CASE WHEN provider = '' THEN 'missing' ELSE provider END,
            identities: identities
        }) AS providers
    """

    repeated_provider_query = """
        MATCH (u:User)-[:HAS_OAUTH_IDENTITY]->(identity:OAuthIdentity)
        WITH
            u,
            toLower(trim(coalesce(identity.provider, ''))) AS provider,
            count(*) AS members
        WHERE members > 1
        RETURN count(*) AS duplicate_groups, coalesce(sum(members), 0) AS affected_identities
    """

    try:
        driver.verify_connectivity()
        with driver.session() as session:
            return session.execute_read(
                lambda transaction: {
                    "users": _safe_record(
                        transaction.run(summary_query).single()
                    ),
                    "duplicate_user_ids": _safe_record(
                        transaction.run(duplicate_user_id_query).single()
                    ),
                    "duplicate_emails": _safe_record(
                        transaction.run(duplicate_email_query).single()
                    ),
                    "duplicate_usernames": _safe_record(
                        transaction.run(duplicate_username_query).single()
                    ),
                    "identity_nodes": _safe_record(
                        transaction.run(relationship_query).single()
                    ),
                    "oauth_provider_breakdown": transaction.run(
                        provider_query
                    ).single()["providers"],
                    "repeated_provider_per_user": _safe_record(
                        transaction.run(repeated_provider_query).single()
                    ),
                }
            )
    finally:
        driver.close()


def _postgres_target_counts() -> dict[str, int]:
    tables = (
        "users",
        "usernames",
        "user_emails",
        "auth_identities",
        "password_credentials",
        "user_profiles",
    )
    counts: dict[str, int] = {}
    with postgres_connection() as connection:
        for table in tables:
            row = connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()
            counts[table] = int(row["count"])
    return counts


def build_preview() -> dict[str, Any]:
    source = _neo4j_preview()
    users = source["users"]
    non_uuid_user_ids = users["with_user_id"] - users["uuid_compatible_user_id"]

    blockers = {
        "missing_user_id": users["missing_user_id"],
        "non_uuid_user_id": non_uuid_user_ids,
        "duplicate_user_id_groups": source["duplicate_user_ids"]["duplicate_groups"],
        "duplicate_email_groups": source["duplicate_emails"]["duplicate_groups"],
        "duplicate_username_groups": source["duplicate_usernames"]["duplicate_groups"],
        "orphan_oauth_identity_nodes": source["identity_nodes"][
            "orphan_oauth_identity_nodes"
        ],
        "orphan_password_identity_nodes": source["identity_nodes"][
            "orphan_password_identity_nodes"
        ],
        "repeated_oauth_provider_groups": source["repeated_provider_per_user"][
            "duplicate_groups"
        ],
    }

    return {
        "mode": "read_only",
        "privacy": "aggregate_counts_only",
        "source": {"neo4j": source},
        "target": {"postgres": _postgres_target_counts()},
        "blockers": blockers,
        "safe_for_automatic_migration": not any(blockers.values()),
        "postgres_writes_performed": 0,
    }


if __name__ == "__main__":
    print(json.dumps(build_preview(), ensure_ascii=False, indent=2, sort_keys=True))
