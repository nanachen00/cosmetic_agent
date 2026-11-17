#!/usr/bin/env python3
"""Initialize only the loopback blinkai database using canonical migrations."""
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2] / "cosmetic-agent"
values = dotenv_values(ROOT / ".env")
database_url = values.get("DATABASE_URL") or ""
url = urlsplit(database_url)
if url.scheme not in {"postgresql", "postgres"} or url.hostname not in {"localhost", "127.0.0.1"}:
    raise SystemExit("DATABASE_URL must name the local Mac PostgreSQL")
role, password = unquote(url.username or ""), unquote(url.password or "")
database, port = url.path.lstrip("/"), url.port or 5432
if role != "blinkai" or database != "blinkai" or not password or port != 5432:
    raise SystemExit("Expected local blinkai role/database at port 5432 with a password")
with psycopg.connect("dbname=postgres", autocommit=True) as admin:
    if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    if not admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(role)))
with psycopg.connect(database_url, autocommit=True) as connection:
    for migration in sorted((ROOT / "database/postgres/migrations").glob("*.sql")):
        connection.execute(migration.read_text())
        print(f"[OK] migration {migration.name}")
    count = connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]
    print(f"[OK] local database public_tables={count}")
