"""Durable, owner-scoped PostgreSQL mirror for chat messages."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_connection, postgres_jsonb


class PostgresConversationMessageRepository:
    def append(
        self,
        *,
        user_id: str,
        conversation_id: str,
        role: str,
        content: str,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "api",
        external_message_id: str | None = None,
    ) -> bool:
        canonical_user_id = UUID(str(user_id))
        normalized_conversation_id = (conversation_id or "default").strip() or "default"
        normalized_role = (role or "").strip().lower()
        normalized_content = (content or "").strip()
        if normalized_role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not normalized_content:
            raise ValueError("content must not be empty")

        with postgres_connection() as connection:
            result = connection.execute(
                """
                INSERT INTO conversation_messages (
                    conversation_id, role, content, request_id,
                    source, external_message_id, metadata
                )
                SELECT
                    conversations.id, %s, %s, %s, %s, %s, %s
                FROM conversations
                WHERE conversations.user_id = %s
                  AND conversations.client_conversation_id = %s
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    normalized_role,
                    normalized_content,
                    (request_id or "").strip() or None,
                    source,
                    (external_message_id or "").strip() or None,
                    postgres_jsonb(metadata or {}),
                    canonical_user_id,
                    normalized_conversation_id,
                ),
            ).fetchone()
        return result is not None

    def history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        lastn: int = 50,
    ) -> list[dict[str, Any]]:
        canonical_user_id = UUID(str(user_id))
        normalized_conversation_id = (conversation_id or "default").strip() or "default"
        safe_limit = max(1, min(int(lastn), 200))
        with postgres_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, request_id, source, metadata, created_at
                FROM (
                    SELECT
                        conversation_messages.id,
                        conversation_messages.role,
                        conversation_messages.content,
                        conversation_messages.request_id,
                        conversation_messages.source,
                        conversation_messages.metadata,
                        conversation_messages.created_at
                    FROM conversation_messages
                    JOIN conversations
                      ON conversations.id = conversation_messages.conversation_id
                    WHERE conversations.user_id = %s
                      AND conversations.client_conversation_id = %s
                    ORDER BY conversation_messages.id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY id ASC
                """,
                (canonical_user_id, normalized_conversation_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self, *, user_id: str, conversation_id: str) -> int:
        canonical_user_id = UUID(str(user_id))
        normalized_conversation_id = (conversation_id or "default").strip() or "default"
        with postgres_connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM conversation_messages
                JOIN conversations
                  ON conversations.id = conversation_messages.conversation_id
                WHERE conversations.user_id = %s
                  AND conversations.client_conversation_id = %s
                """,
                (canonical_user_id, normalized_conversation_id),
            ).fetchone()
        return int(row["count"] if row else 0)


postgres_conversation_message_repository = PostgresConversationMessageRepository()
