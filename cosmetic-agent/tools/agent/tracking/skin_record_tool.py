"""
Skin Record Tool（Neo4j 版 CRUD + 報表資格）
連線一律讀環境變數：NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD，不要硬編碼。
"""
from __future__ import annotations

import os
import uuid
from datetime import date as Date
from typing import Any, Optional

from agents.skin_tracking.observation_taxonomy import (
    allowed_detail_zones,
    catalog_by_slug,
    catalog_seed_payload,
    validate_detail_zones,
    validate_tag_slugs,
)


def normalize_record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Add safe defaults for legacy Neo4j nodes without mutating driver data."""
    normalized = dict(record)
    known_tags = catalog_by_slug()
    known_zones = allowed_detail_zones()
    normalized["tag_slugs"] = list(dict.fromkeys(
        str(value) for value in (normalized.get("tag_slugs") or []) if str(value) in known_tags
    ))
    normalized["tags"] = [{"slug": slug, "display_name": known_tags[slug].name_zh_tw, "category": known_tags[slug].category} for slug in normalized["tag_slugs"]]
    normalized["detail_zones"] = list(dict.fromkeys(
        str(value) for value in (normalized.get("detail_zones") or []) if str(value) in known_zones
    ))
    return normalized


def longest_recent_streak(dates: list[Date], today: Date) -> tuple[int, list[Date]]:
    """從最近一筆往回數的連續 distinct 天數。
    最近一筆必須是今天或昨天才算「進行中」，否則 streak=0。"""
    if not dates:
        return 0, []
    dates = sorted(set(dates))
    last = dates[-1]
    if (today - last).days > 1:
        return 0, []
    streak = [last]
    for d in reversed(dates[:-1]):
        if (streak[-1] - d).days == 1:
            streak.append(d)
        else:
            break
    return len(streak), sorted(streak)


class SkinRecordStore:
    """所有 Cypher 集中在這；tracking_agent 只呼叫方法。"""

    SCHEMA = [
        "CREATE CONSTRAINT skin_user_id IF NOT EXISTS FOR (u:User) REQUIRE u.user_id IS UNIQUE",
        "CREATE CONSTRAINT skin_record_id IF NOT EXISTS FOR (r:SkinRecord) REQUIRE r.record_id IS UNIQUE",
        "CREATE CONSTRAINT user_product_name IF NOT EXISTS FOR (p:UserProduct) REQUIRE p.name IS UNIQUE",
        "CREATE CONSTRAINT skin_observation_tag_slug IF NOT EXISTS FOR (t:SkinObservationTag) REQUIRE t.slug IS UNIQUE",
        "CREATE CONSTRAINT face_subregion_slug IF NOT EXISTS FOR (z:FaceSubregion) REQUIRE z.slug IS UNIQUE",
    ]

    def __init__(self, uri: str = None, user: str = None, password: str = None):
        from neo4j import GraphDatabase  # lazy import，測試可用 FakeStore 取代
        self.driver = GraphDatabase.driver(
            uri or os.environ["NEO4J_URI"],
            auth=(user or os.environ["NEO4J_USER"],
                  password or os.environ["NEO4J_PASSWORD"]),
        )

    def close(self):
        self.driver.close()

    def verify_connectivity(self):
        """Check the configured Neo4j endpoint without running Cypher or writing data."""
        self.driver.verify_connectivity()

    def init_schema(self):
        with self.driver.session() as s:
            for stmt in self.SCHEMA:
                s.run(stmt)
        self.seed_observation_catalog()

    def seed_observation_catalog(self) -> None:
        """Idempotently seed the controlled 100-tag catalog and 27 subregions."""
        cypher = """
        UNWIND $tags AS tag
        MERGE (t:SkinObservationTag {slug: tag.slug})
        SET t.name_zh_tw = tag.name_zh_tw, t.category = tag.category,
            t.kind = tag.kind, t.condition_flag = tag.condition_flag,
            t.clinical_review_required = tag.clinical_review_required,
            t.source_urls = tag.source_urls, t.aliases = tag.aliases,
            t.updated_at = datetime()
        WITH t, tag
        OPTIONAL MATCH (t)-[old:APPLIES_TO]->(:FaceSubregion)
        DELETE old
        WITH t, tag
        UNWIND tag.face_regions AS region_slug
        MERGE (z:FaceSubregion {slug: region_slug})
        MERGE (t)-[:APPLIES_TO]->(z)
        """
        with self.driver.session() as s:
            s.run(cypher, tags=catalog_seed_payload()).consume()

    # ---------- CRUD ----------

    def create(self, user_id: str, rec: dict[str, Any]) -> str:
        record_id = str(uuid.uuid4())
        cypher = """
        MERGE (u:User {user_id: $user_id})
        CREATE (r:SkinRecord {
            record_id: $record_id, date: date($date),
            issue: $issue, severity: $severity, zone: $zone,
            tag_slugs: $tag_slugs, detail_zones: $detail_zones,
            onset_date: CASE WHEN $onset_date IS NULL THEN NULL ELSE date($onset_date) END,
            duration_days: $duration_days, note: $note,
            record_status: $record_status, stability_source: $stability_source,
            created_at: datetime(), updated_at: datetime()
        })
        MERGE (u)-[:HAS_SKIN_RECORD]->(r)
        WITH r
        UNWIND $products AS pname
          MERGE (p:UserProduct {name: pname})
          MERGE (r)-[:USED_PRODUCT]->(p)
        """
        with self.driver.session() as s:
            s.run(cypher, user_id=user_id, record_id=record_id,
                  date=str(rec["date"]), issue=rec["issue"],
                  severity=rec["severity"], zone=rec["zone"],
                  tag_slugs=validate_tag_slugs(rec.get("tag_slugs")),
                  detail_zones=validate_detail_zones(rec.get("detail_zones")),
                  onset_date=str(rec["onset_date"]) if rec.get("onset_date") else None,
                  duration_days=rec.get("duration_days"),
                  note=rec.get("note", ""), products=rec.get("products") or [],
                  record_status=rec.get("record_status", "recorded"),
                  stability_source=rec.get("stability_source"))
        return record_id

    def query(self, user_id: str, date_from: Optional[str] = None,
              date_to: Optional[str] = None, issue: Optional[str] = None,
              zone: Optional[str] = None) -> list[dict]:
        cypher = """
        MATCH (u:User {user_id: $user_id})-[:HAS_SKIN_RECORD]->(r:SkinRecord)
        WHERE ($date_from IS NULL OR r.date >= date($date_from))
          AND ($date_to   IS NULL OR r.date <= date($date_to))
          AND ($issue     IS NULL OR r.issue = $issue)
          AND ($zone      IS NULL OR r.zone = $zone)
        OPTIONAL MATCH (r)-[:USED_PRODUCT]->(p:UserProduct)
        RETURN r, collect(p.name) AS products
        ORDER BY r.date DESC, r.created_at DESC
        """
        with self.driver.session() as s:
            rows = s.run(cypher, user_id=user_id, date_from=date_from,
                         date_to=date_to, issue=issue, zone=zone)
            out = []
            for row in rows:
                d = normalize_record_metadata(dict(row["r"]))
                for k in ("date", "onset_date"):
                    if d.get(k) is not None:
                        d[k] = d[k].to_native()
                d.pop("created_at", None); d.pop("updated_at", None)
                d["products"] = [p for p in row["products"] if p]
                d["record_status"] = d.get("record_status") or "recorded"
                d["stability_source"] = d.get("stability_source")
                out.append(d)
            return out

    def update(self, user_id: str, record_id: str, fields: dict[str, Any]) -> bool:
        allowed = {
            "issue",
            "severity",
            "zone",
            "tag_slugs",
            "detail_zones",
            "onset_date",
            "duration_days",
            "note",
            "date",
            "record_status",
            "stability_source",
        }
        sets, params = [], {"user_id": user_id, "record_id": record_id}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "tag_slugs":
                v = validate_tag_slugs(v)
            elif k == "detail_zones":
                v = validate_detail_zones(v)
            if k == "record_status" and v == "recorded":
                sets.append("r.stability_source = NULL")
            if k in ("date", "onset_date"):
                sets.append(f"r.{k} = date(${k})"); params[k] = str(v)
            else:
                sets.append(f"r.{k} = ${k}"); params[k] = v
        if not sets:
            return False
        cypher = f"""
        MATCH (:User {{user_id: $user_id}})-[:HAS_SKIN_RECORD]->(r:SkinRecord {{record_id: $record_id}})
        SET {', '.join(sets)}, r.updated_at = datetime()
        RETURN r.record_id AS id
        """
        with self.driver.session() as s:
            return s.run(cypher, params).single() is not None

    def delete(self, user_id: str, record_id: str) -> bool:
        cypher = """
        MATCH (:User {user_id: $user_id})-[:HAS_SKIN_RECORD]->(r:SkinRecord {record_id: $record_id})
        DETACH DELETE r RETURN count(*) AS n
        """
        with self.driver.session() as s:
            row = s.run(cypher, user_id=user_id, record_id=record_id).single()
            return bool(row and row["n"] > 0)

    # ---------- 報表資格 ----------

    def distinct_dates(self, user_id: str) -> list[Date]:
        cypher = """
        MATCH (:User {user_id: $user_id})-[:HAS_SKIN_RECORD]->(r:SkinRecord)
        RETURN DISTINCT r.date AS d ORDER BY d
        """
        with self.driver.session() as s:
            return [row["d"].to_native() for row in s.run(cypher, user_id=user_id)]

    def check_eligibility(self, user_id: str, today: Date = None, min_days: int = 5) -> dict:
        today = today or Date.today()
        streak, streak_dates = longest_recent_streak(self.distinct_dates(user_id), today)
        return {
            "eligible": streak >= min_days,
            "consecutive_days": streak,
            "days_needed": max(0, min_days - streak),
            "streak_dates": [str(d) for d in streak_dates],
        }
