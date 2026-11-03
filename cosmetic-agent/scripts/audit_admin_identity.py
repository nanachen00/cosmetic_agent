import json
import sys
import hashlib

from dotenv import dotenv_values, load_dotenv
from neo4j import GraphDatabase


ROOT = "/var/lib/jenkins/tomod/cosmetic-agent"
sys.path.insert(0, ROOT)
load_dotenv(f"{ROOT}/.env")

from database.postgres.connection import postgres_connection


username = "czc.20"

with postgres_connection() as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT u.id::text AS user_id, n.username
            FROM users u
            JOIN usernames n ON n.user_id = u.id
            WHERE lower(n.username) = lower(%s)
              AND u.deleted_at IS NULL
            """,
            (username,),
        )
        postgres_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT lower(e.email) AS email
            FROM user_emails e
            JOIN usernames n ON n.user_id = e.user_id
            WHERE lower(n.username) = lower(%s)
            """,
            (username,),
        )
        postgres_emails = [row["email"] for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT a.provider, a.provider_subject
            FROM auth_identities a
            JOIN usernames n ON n.user_id = a.user_id
            WHERE lower(n.username) = lower(%s)
            """,
            (username,),
        )
        postgres_identities = [dict(row) for row in cursor.fetchall()]

config = dotenv_values(f"{ROOT}/.env")
driver = GraphDatabase.driver(
    config["NEO4J_URI"],
    auth=(config["NEO4J_USER"], config["NEO4J_PASSWORD"]),
)
with driver.session() as session:
    neo4j_rows = session.run(
        """
        MATCH (u:User)
        WHERE toLower(coalesce(u.username, '')) = toLower($username)
        RETURN u.user_id AS user_id, u.username AS username, u.email AS email
        """,
        username=username,
    ).data()
    neo4j_identity_rows = session.run(
        """
        MATCH (u:User)-[:HAS_OAUTH_IDENTITY]->(i:OAuthIdentity)
        WHERE toLower(coalesce(u.username, '')) = toLower($username)
        RETURN properties(i) AS properties
        """,
        username=username,
    ).data()
driver.close()


def digest(value):
    return hashlib.sha256(str(value).strip().lower().encode()).hexdigest()


neo4j_emails = []
neo4j_identities = []
for row in neo4j_rows:
    if row.get("email"):
        neo4j_emails.append(row["email"])
for row in neo4j_identity_rows:
    properties = row["properties"]
    email = properties.get("email")
    if email:
        neo4j_emails.append(email)
    provider = properties.get("provider")
    subject = (
        properties.get("provider_subject")
        or properties.get("provider_user_id")
        or properties.get("subject")
        or properties.get("sub")
    )
    if provider and subject:
        neo4j_identities.append({"provider": provider, "provider_subject": subject})

postgres_email_hashes = {digest(value) for value in postgres_emails}
neo4j_email_hashes = {digest(value) for value in neo4j_emails}
postgres_identity_hashes = {
    (str(row["provider"]).lower(), digest(row["provider_subject"]))
    for row in postgres_identities
}
neo4j_identity_hashes = {
    (str(row["provider"]).lower(), digest(row["provider_subject"]))
    for row in neo4j_identities
}

print(json.dumps({
    "postgres": [dict(row) for row in postgres_rows],
    "neo4j": [
        {"user_id": row["user_id"], "username": row["username"]}
        for row in neo4j_rows
    ],
    "same_user_id": (
        len(postgres_rows) == 1
        and len(neo4j_rows) == 1
        and postgres_rows[0]["user_id"] == neo4j_rows[0]["user_id"]
    ),
    "has_matching_email": bool(postgres_email_hashes & neo4j_email_hashes),
    "matching_oauth_providers": sorted({
        provider
        for provider, subject_hash in postgres_identity_hashes
        if (provider, subject_hash) in neo4j_identity_hashes
    }),
    "neo4j_oauth_property_keys": sorted({
        key for row in neo4j_identity_rows for key in row["properties"].keys()
    }),
}, ensure_ascii=False, indent=2))
