"""Verify development PostgreSQL without printing credentials."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.postgres import check_postgres_connection


if __name__ == "__main__":
    print(json.dumps(check_postgres_connection(), ensure_ascii=False, sort_keys=True))
