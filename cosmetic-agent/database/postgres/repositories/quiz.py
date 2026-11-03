"""PostgreSQL storage for member quiz attempts and answers."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from database.postgres.connection import postgres_jsonb

from database.postgres.connection import postgres_connection


class PostgresQuizRepository:
    def record(
        self,
        *,
        user_id: str,
        quiz_version: str,
        selected_answers: dict[str, str],
        quiz_result: dict[str, Any],
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        canonical_user_id = UUID(str(user_id))
        event_id = uuid4()
        result_data = {
            **quiz_result,
            "event_id": str(event_id),
            "session_id": session_id,
            "message_id": message_id,
        }
        with postgres_connection() as connection:
            attempt_id = connection.execute(
                """
                INSERT INTO quiz_attempts (
                    user_id, quiz_key, quiz_version, status,
                    result_data, completed_at
                ) VALUES (%s, 'customer_type', %s, 'completed', %s, now())
                RETURNING id
                """,
                (canonical_user_id, quiz_version, postgres_jsonb(result_data)),
            ).fetchone()["id"]
            for question_key, answer in selected_answers.items():
                connection.execute(
                    "INSERT INTO quiz_answers "
                    "(attempt_id, question_key, answer_data) VALUES (%s, %s, %s)",
                    (attempt_id, question_key, postgres_jsonb({"answer_id": answer})),
                )
            connection.execute(
                "INSERT INTO audit_events "
                "(id, actor_user_id, event_type, target_type, target_id) "
                "VALUES (%s, %s, 'quiz_completed', 'quiz_attempt', %s)",
                (event_id, canonical_user_id, str(attempt_id)),
            )
        return {
            "ok": True,
            "event_id": str(event_id),
            "event_mongo_id": None,
            "quiz_result_id": str(attempt_id),
            "quiz_result_mongo_id": None,
            "quiz_version": quiz_version,
            **quiz_result,
        }

    def latest(self, *, user_id: str) -> dict[str, Any]:
        canonical_user_id = UUID(str(user_id))
        with postgres_connection() as connection:
            row = connection.execute(
                """
                SELECT id, quiz_version, result_data, completed_at
                FROM quiz_attempts
                WHERE user_id = %s AND quiz_key = 'customer_type'
                  AND status = 'completed'
                ORDER BY completed_at DESC NULLS LAST, started_at DESC
                LIMIT 1
                """,
                (canonical_user_id,),
            ).fetchone()
        if row is None:
            return {"ok": True, "has_completed": False, "result": None}
        result_data = row["result_data"] or {}
        return {
            "ok": True,
            "has_completed": True,
            "result": {
                "quiz_result_id": str(row["id"]),
                "quiz_version": row["quiz_version"],
                "primary_type": result_data.get("primary_type"),
                "secondary_type": result_data.get("secondary_type"),
                "scores": result_data.get("scores", {}),
                "confidence": result_data.get("confidence", 0),
                "completed_at": (
                    row["completed_at"].isoformat() if row["completed_at"] else None
                ),
            },
        }


postgres_quiz_repository = PostgresQuizRepository()
