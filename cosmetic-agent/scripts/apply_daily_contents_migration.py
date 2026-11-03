from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, "/var/lib/jenkins/tomod/cosmetic-agent")

from database.postgres.connection import postgres_connection


load_dotenv("/var/lib/jenkins/tomod/cosmetic-agent/.env")
migration = Path(
    "/var/lib/jenkins/tomod/cosmetic-agent/database/postgres/migrations/0004_daily_contents.sql"
)

with postgres_connection(autocommit=True) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.daily_contents') AS table_name")
        exists = cursor.fetchone()["table_name"] is not None
        if not exists:
            cursor.execute(migration.read_text(encoding="utf-8"))

        cursor.execute(
            """
            SELECT content_type, review_status, is_active, count(*) AS count
            FROM daily_contents
            GROUP BY content_type, review_status, is_active
            ORDER BY content_type, review_status, is_active
            """
        )
        print({"already_existed": exists, "counts": cursor.fetchall()})
