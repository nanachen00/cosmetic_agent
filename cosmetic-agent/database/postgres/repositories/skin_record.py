"""PostgreSQL implementation of the existing SkinRecordStore interface."""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Optional
from uuid import UUID

from database.postgres.connection import postgres_jsonb

from database.postgres.connection import postgres_connection
from tools.agent.tracking.skin_record_tool import longest_recent_streak


class PostgresSkinRecordStore:
    def close(self) -> None:
        return None

    def verify_connectivity(self) -> None:
        with postgres_connection() as connection:
            connection.execute("SELECT 1")

    def init_schema(self) -> None:
        return None

    @staticmethod
    def _uuid(value: str) -> UUID:
        return UUID(str(value))

    def create(self, user_id: str, rec: dict[str, Any]) -> str:
        canonical_user_id = self._uuid(user_id)
        record_data = {
            "date": str(rec["date"]),
            "zone": rec["zone"],
            "duration_days": rec.get("duration_days"),
            "note": rec.get("note", ""),
            "products": rec.get("products") or [],
            "record_status": rec.get("record_status", "recorded"),
            "stability_source": rec.get("stability_source"),
        }
        with postgres_connection() as connection:
            record_id = connection.execute(
                """
                INSERT INTO skin_records (
                    user_id, concern, severity, started_on, record_data
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    canonical_user_id,
                    rec["issue"],
                    rec["severity"],
                    rec.get("onset_date"),
                    postgres_jsonb(record_data),
                ),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO skin_record_areas (skin_record_id, area_code) "
                "VALUES (%s, %s)",
                (record_id, rec["zone"]),
            )
        return str(record_id)

    @staticmethod
    def _document(row: dict[str, Any]) -> dict[str, Any]:
        data = row["record_data"] or {}
        return {
            "record_id": str(row["id"]),
            "date": Date.fromisoformat(data["date"]),
            "issue": row["concern"],
            "severity": row["severity"],
            "zone": data.get("zone", ""),
            "onset_date": row["started_on"],
            "duration_days": data.get("duration_days"),
            "note": data.get("note", ""),
            "products": list(data.get("products", [])),
            "record_status": data.get("record_status", "recorded"),
            "stability_source": data.get("stability_source"),
        }

    def query(
        self,
        user_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        issue: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = %s"]
        parameters: list[Any] = [self._uuid(user_id)]
        if date_from:
            clauses.append("(record_data->>'date')::date >= %s")
            parameters.append(Date.fromisoformat(date_from))
        if date_to:
            clauses.append("(record_data->>'date')::date <= %s")
            parameters.append(Date.fromisoformat(date_to))
        if issue:
            clauses.append("concern = %s")
            parameters.append(issue)
        if zone:
            clauses.append("record_data->>'zone' = %s")
            parameters.append(zone)
        with postgres_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skin_records WHERE " + " AND ".join(clauses)
                + " ORDER BY (record_data->>'date')::date DESC, created_at DESC",
                tuple(parameters),
            ).fetchall()
        return [self._document(row) for row in rows]

    def update(self, user_id: str, record_id: str, fields: dict[str, Any]) -> bool:
        canonical_user_id = self._uuid(user_id)
        record_uuid = self._uuid(record_id)
        with postgres_connection() as connection:
            row = connection.execute(
                "SELECT concern, severity, started_on, record_data FROM skin_records "
                "WHERE id = %s AND user_id = %s FOR UPDATE",
                (record_uuid, canonical_user_id),
            ).fetchone()
            if row is None:
                return False
            data = dict(row["record_data"] or {})
            concern = fields.get("issue", row["concern"])
            severity = fields.get("severity", row["severity"])
            started_on = fields.get("onset_date", row["started_on"])
            for key in (
                "zone", "duration_days", "note", "date",
                "record_status", "stability_source",
            ):
                if key in fields and fields[key] is not None:
                    data[key] = str(fields[key]) if key == "date" else fields[key]
            if fields.get("record_status") == "recorded":
                data["stability_source"] = None
            connection.execute(
                "UPDATE skin_records SET concern = %s, severity = %s, "
                "started_on = %s, record_data = %s, updated_at = now() "
                "WHERE id = %s AND user_id = %s",
                (
                    concern, severity, started_on, postgres_jsonb(data),
                    record_uuid, canonical_user_id,
                ),
            )
            if "zone" in fields:
                connection.execute(
                    "DELETE FROM skin_record_areas WHERE skin_record_id = %s",
                    (record_uuid,),
                )
                connection.execute(
                    "INSERT INTO skin_record_areas (skin_record_id, area_code) "
                    "VALUES (%s, %s)",
                    (record_uuid, fields["zone"]),
                )
        return True

    def delete(self, user_id: str, record_id: str) -> bool:
        with postgres_connection() as connection:
            result = connection.execute(
                "DELETE FROM skin_records WHERE id = %s AND user_id = %s",
                (self._uuid(record_id), self._uuid(user_id)),
            )
        return result.rowcount > 0

    def distinct_dates(self, user_id: str) -> list[Date]:
        with postgres_connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT (record_data->>'date')::date AS value "
                "FROM skin_records WHERE user_id = %s ORDER BY value",
                (self._uuid(user_id),),
            ).fetchall()
        return [row["value"] for row in rows]

    def check_eligibility(
        self, user_id: str, today: Date | None = None, min_days: int = 5
    ) -> dict[str, Any]:
        today = today or Date.today()
        streak, dates = longest_recent_streak(self.distinct_dates(user_id), today)
        return {
            "eligible": streak >= min_days,
            "consecutive_days": streak,
            "days_needed": max(0, min_days - streak),
            "streak_dates": [str(value) for value in dates],
        }


postgres_skin_record_store = PostgresSkinRecordStore()
