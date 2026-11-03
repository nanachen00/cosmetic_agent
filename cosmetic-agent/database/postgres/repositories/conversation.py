"""Canonical PostgreSQL mapping for BLINK AI users and GetZep threads."""

from __future__ import annotations

import hashlib
from uuid import UUID

from database.postgres.connection import postgres_connection


class PostgresConversationRepository:
    @staticmethod
    def thread_id(user_id: str, conversation_id: str) -> str:
        source = f"{user_id}:{conversation_id}"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:48]
        return f"blink_chat_{digest}"

    def ensure(self, *, user_id: str, conversation_id: str) -> str:
        canonical_user_id = UUID(str(user_id))
        normalized_conversation_id = (conversation_id or "default").strip() or "default"
        thread_id = self.thread_id(str(canonical_user_id), normalized_conversation_id)
        with postgres_connection() as connection:
            connection.execute(
                """
                INSERT INTO external_memory_links (
                    user_id, provider, external_user_id
                ) VALUES (%s, 'zep', %s)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    external_user_id = EXCLUDED.external_user_id
                """,
                (canonical_user_id, str(canonical_user_id)),
            )
            connection.execute(
                """
                INSERT INTO conversations (
                    user_id, zep_session_id, client_conversation_id
                ) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, client_conversation_id)
                    WHERE client_conversation_id IS NOT NULL
                DO UPDATE SET last_message_at = now()
                """,
                (canonical_user_id, thread_id, normalized_conversation_id),
            )
        return thread_id

    def delete(self, *, user_id: str, conversation_id: str) -> None:
        with postgres_connection() as connection:
            connection.execute(
                "DELETE FROM conversations "
                "WHERE user_id = %s AND client_conversation_id = %s",
                (UUID(str(user_id)), conversation_id),
            )


postgres_conversation_repository = PostgresConversationRepository()
