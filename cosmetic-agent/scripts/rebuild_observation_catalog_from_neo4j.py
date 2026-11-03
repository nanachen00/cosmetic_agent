"""Rebuild agents/skin_tracking/skin_observation_labels_100.json from Neo4j Aura.

Added 2026-10-21. The JSON catalog was lost with the container, but
tools/agent/tracking/skin_record_tool.py seeded every label into Neo4j as
(:SkinObservationTag) nodes with the same fields, so Aura can restore it.

Usage (on the server, with NEO4J_* in the environment):
    python scripts/rebuild_observation_catalog_from_neo4j.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from neo4j import GraphDatabase

TARGET = Path(__file__).resolve().parents[1] / "agents" / "skin_tracking" / "skin_observation_labels_100.json"

QUERY = """
MATCH (t:SkinObservationTag)
RETURN t.slug AS slug, t.name_zh_tw AS name_zh_tw, t.category AS category,
       coalesce(t.condition_flag, false) AS condition_flag,
       coalesce(t.source_urls, []) AS source_urls,
       coalesce(t.face_regions, []) AS face_regions
ORDER BY t.slug
"""


def main() -> int:
    uri, user, password = (os.environ.get(k, "").strip() for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"))
    if not (uri and user and password):
        print("NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD are required", file=sys.stderr)
        return 2
    database = os.environ.get("NEO4J_DATABASE", "").strip() or None
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session(database=database) as session:
            rows = [dict(record) for record in session.run(QUERY)]

    if len(rows) < 100:
        print(f"Only {len(rows)} SkinObservationTag nodes found; the catalog needs at least 100.", file=sys.stderr)
        print("Check whether the tags were ever seeded (Neo4j skin-record mode) before relying on this.", file=sys.stderr)
        return 1
    if TARGET.exists() and "--force" not in sys.argv:
        print(f"{TARGET} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    TARGET.write_text(
        json.dumps({"source": "rebuilt-from-neo4j", "labels": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(rows)} labels to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
