#!/usr/bin/env python3
"""Idempotently import the reviewed Taiwan lexicon JSON files into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path
from typing import Any

import psycopg


DATASETS = {
    "beauty_slang": "tw_beauty_slang_100.json",
    "facial_drug_alias": "tw_facial_drug_aliases_100.json",
}


def normalize_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return "".join(normalized.split())


def load_dataset(base_dir: Path, dataset: str) -> dict[str, Any]:
    path = base_dir / DATASETS[dataset]
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) != 100:
        raise ValueError(f"{path}: expected exactly 100 entries")

    ids = [entry.get("id") for entry in entries]
    if ids != list(range(1, 101)):
        raise ValueError(f"{path}: ids must be consecutive from 1 to 100")

    normalized_terms = [normalize_term(str(entry.get("term", ""))) for entry in entries]
    if any(not term for term in normalized_terms):
        raise ValueError(f"{path}: empty term found")
    if len(set(normalized_terms)) != len(normalized_terms):
        raise ValueError(f"{path}: duplicate normalized term found")

    return document


def to_row(dataset: str, version: str, entry: dict[str, Any]) -> tuple[Any, ...]:
    if dataset == "beauty_slang":
        category = entry["category"]
        canonical = None
        meaning = entry["meaning"]
        handling = entry["handling"]
        risk = None
        action = None
    else:
        category = entry["kind"]
        canonical = entry["maps_to"]
        meaning = None
        handling = None
        risk = entry["risk"]
        action = entry["action"]

    return (
        dataset,
        entry["id"],
        entry["term"],
        normalize_term(entry["term"]),
        category,
        canonical,
        meaning,
        handling,
        risk,
        action,
        entry.get("note", ""),
        version,
        json.dumps(entry, ensure_ascii=False),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default="postgresql:///blinkai_dev")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()

    documents = {
        dataset: load_dataset(args.data_dir, dataset)
        for dataset in DATASETS
    }

    upsert_term = """
        INSERT INTO taiwan_local_terms (
            dataset, dataset_item_id, term, normalized_term, category,
            canonical, meaning, handling, risk, action, note, version, payload
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT (dataset, dataset_item_id, version) DO UPDATE SET
            term = EXCLUDED.term,
            normalized_term = EXCLUDED.normalized_term,
            category = EXCLUDED.category,
            canonical = EXCLUDED.canonical,
            meaning = EXCLUDED.meaning,
            handling = EXCLUDED.handling,
            risk = EXCLUDED.risk,
            action = EXCLUDED.action,
            note = EXCLUDED.note,
            payload = EXCLUDED.payload,
            active = TRUE,
            updated_at = NOW()
    """

    upsert_source = """
        INSERT INTO taiwan_lexicon_sources (dataset, name, url, version)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (dataset, url, version) DO UPDATE SET name = EXCLUDED.name
    """

    with psycopg.connect(args.database_url) as connection:
        with connection.cursor() as cursor:
            for dataset, document in documents.items():
                version = document["version"]
                cursor.executemany(
                    upsert_term,
                    [to_row(dataset, version, entry) for entry in document["entries"]],
                )
                for source in document.get("sources", []):
                    if isinstance(source, str):
                        source_name = f"{dataset} source"
                        source_url = source
                    else:
                        source_name = source["name"]
                        source_url = source["url"]
                    cursor.execute(
                        upsert_source,
                        (dataset, source_name, source_url, version),
                    )

            cursor.execute(
                """
                SELECT dataset, COUNT(*)
                FROM taiwan_local_terms
                WHERE active = TRUE
                GROUP BY dataset
                ORDER BY dataset
                """
            )
            counts = dict(cursor.fetchall())

    expected = {"beauty_slang": 100, "facial_drug_alias": 100}
    if counts != expected:
        raise RuntimeError(f"unexpected active counts: {counts!r}")

    print(json.dumps({"status": "ok", "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
