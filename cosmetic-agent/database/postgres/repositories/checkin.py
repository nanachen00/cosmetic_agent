"""Small Mongo-compatible facade for the existing check-in business rules."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_jsonb

from database.postgres.connection import postgres_connection


class _Cursor(list[dict[str, Any]]):
    def sort(self, keys):
        for key, direction in reversed(keys):
            super().sort(key=lambda item: item.get(key) or "", reverse=direction < 0)
        return self


class PostgresDailyCheckinCollection:
    @staticmethod
    def _uuid(value: str) -> UUID:
        return UUID(str(value))

    @staticmethod
    def _document(row: dict[str, Any]) -> dict[str, Any]:
        metadata = row["skin_status"] or {}
        checked_at = metadata.get("checked_in_at")
        return {
            "user_id": str(row["user_id"]),
            "date": row["checkin_date"].isoformat(),
            "created_at": row["created_at"].isoformat(),
            "checked_in_at": checked_at or row["created_at"].isoformat(),
            "timezone": metadata.get("timezone", "Asia/Taipei"),
            "points_earned": int(metadata.get("points_earned", 10)),
            "cycle_number": metadata.get("cycle_number"),
            "day_number": metadata.get("day_number"),
        }

    def find(self, query: dict[str, Any], projection=None) -> _Cursor:
        del projection
        user_id = self._uuid(query["user_id"])
        with postgres_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM daily_checkins WHERE user_id = %s "
                "ORDER BY checkin_date, created_at",
                (user_id,),
            ).fetchall()
        return _Cursor(self._document(row) for row in rows)

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        user_id = self._uuid(query["user_id"])
        checkin_date = date.fromisoformat(str(query["date"]))
        with postgres_connection() as connection:
            row = connection.execute(
                "SELECT * FROM daily_checkins "
                "WHERE user_id = %s AND checkin_date = %s",
                (user_id, checkin_date),
            ).fetchone()
        return self._document(row) if row else None

    def insert_one(self, document: dict[str, Any]) -> bool:
        user_id = self._uuid(document["user_id"])
        checkin_date = date.fromisoformat(str(document["date"]))
        created_at = datetime.fromisoformat(str(document["created_at"]))
        metadata = {
            "checked_in_at": document.get("checked_in_at"),
            "timezone": document.get("timezone", "Asia/Taipei"),
            "points_earned": int(document.get("points_earned", 10)),
            "cycle_number": document.get("cycle_number"),
            "day_number": document.get("day_number"),
        }
        with postgres_connection() as connection:
            inserted = connection.execute(
                """
                INSERT INTO daily_checkins (
                    user_id, checkin_date, skin_status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, checkin_date) DO NOTHING
                RETURNING id
                """,
                (user_id, checkin_date, postgres_jsonb(metadata), created_at, created_at),
            ).fetchone()
        return inserted is not None


postgres_daily_checkin_collection = PostgresDailyCheckinCollection()
