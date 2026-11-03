"""Backfill search embeddings for Product nodes that do not have one yet.

Dry-run is the default.  Pass ``--apply`` to write generated vectors to Neo4j.
The command is idempotent because it only reads Product nodes whose embedding
property is null.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from FlagEmbedding import BGEM3FlagModel
from neo4j import GraphDatabase


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _search_text(product: dict) -> str:
    values = [
        product.get("name"),
        product.get("brand"),
        product.get("category"),
        " ".join(product.get("ingredients_list") or []),
    ]
    return " ".join(str(value).strip() for value in values if str(value or "").strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    uri = os.getenv("NEO4J_URI", "").strip()
    user = os.getenv("NEO4J_USER", "").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()
    if not all((uri, user, password)):
        raise RuntimeError("NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD must be configured")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            products = session.run(
                """
                MATCH (p:Product)
                WHERE p.embedding IS NULL AND trim(coalesce(p.name, '')) <> ''
                RETURN elementId(p) AS id,
                       p.name AS name,
                       p.brand AS brand,
                       p.category AS category,
                       p.ingredients_list AS ingredients_list
                ORDER BY id
                """
            ).data()

        print(f"missing_product_embeddings={len(products)}")
        if not products or not args.apply:
            if products:
                print("dry_run=true; rerun with --apply to write embeddings")
            return 0

        model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
        texts = [_search_text(product) for product in products]
        vectors = model.encode(texts, batch_size=args.batch_size)["dense_vecs"]

        rows = [
            {"id": product["id"], "embedding": vector.tolist()}
            for product, vector in zip(products, vectors)
        ]
        with driver.session() as session:
            result = session.run(
                """
                UNWIND $rows AS row
                MATCH (p:Product)
                WHERE elementId(p) = row.id AND p.embedding IS NULL
                SET p.embedding = row.embedding
                RETURN count(p) AS updated
                """,
                rows=rows,
            ).single()
        print(f"updated_product_embeddings={result['updated']}")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
