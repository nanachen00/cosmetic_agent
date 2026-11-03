"""Read-only selection of reviewed daily content."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from database.postgres.connection import postgres_connection


load_dotenv("/var/lib/jenkins/tomod/cosmetic-agent/.env")

CONTENT_TYPES = ("beauty_tip", "dermatologist_faq")
TAIPEI = ZoneInfo("Asia/Taipei")


class PostgresDailyContentRepository:
    def get_daily_pair(self, *, day_key: str | None = None) -> dict[str, Any]:
        selection_key = day_key or datetime.now(TAIPEI).date().isoformat()
        selected: dict[str, Any] = {}

        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                for content_type in CONTENT_TYPES:
                    cursor.execute(
                        """
                        SELECT
                            id,
                            content_type,
                            title,
                            summary,
                            source_name,
                            source_url,
                            source_published_at,
                            published_at
                        FROM daily_contents
                        WHERE content_type = %s
                          AND review_status = 'published'
                          AND is_active = TRUE
                        ORDER BY
                            md5(id::text || %s),
                            display_priority DESC
                        LIMIT 1
                        """,
                        (content_type, selection_key),
                    )
                    row = cursor.fetchone()
                    if row:
                        item = dict(row)
                        for key in ("source_published_at", "published_at"):
                            value = item.get(key)
                            item[key] = value.isoformat() if value else None
                        item["id"] = str(item["id"])
                        selected[content_type] = item

        return {
            "ok": True,
            "date": selection_key,
            "contents": selected,
        }


postgres_daily_content_repository = PostgresDailyContentRepository()
