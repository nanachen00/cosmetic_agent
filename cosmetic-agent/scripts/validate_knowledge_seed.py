#!/usr/bin/env python3
"""Validate the v1 knowledge seed without writing to Neo4j."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "knowledge_graph"
    / "v1"
    / "knowledge_seed.json"
)


def validate(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    sources = data.get("sources", [])
    errors: list[str] = []
    warnings: list[str] = []

    node_ids = [str(node.get("id") or "") for node in nodes]
    edge_ids = [str(edge.get("id") or "") for edge in edges]
    source_ids = {str(source.get("id") or "") for source in sources}
    node_id_set = set(node_ids)

    if len(node_ids) != len(node_id_set):
        errors.append("duplicate node ids")
    if len(edge_ids) != len(set(edge_ids)):
        errors.append("duplicate edge ids")
    if Counter(node.get("type") for node in nodes).get("Ingredient") != 100:
        errors.append("v1 must contain exactly 100 Ingredient nodes")
    if Counter(node.get("type") for node in nodes).get("SkinConcern", 0) < 40:
        errors.append("v1 must contain at least 40 SkinConcern nodes")

    degree = Counter()
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in node_id_set or target not in node_id_set:
            errors.append(f"missing endpoint: {edge.get('id')}")
        degree[source] += 1
        degree[target] += 1
        unknown_sources = set(edge.get("source_ids") or []) - source_ids
        if unknown_sources:
            errors.append(
                f"unknown source ids on {edge.get('id')}: {sorted(unknown_sources)}"
            )
        if not edge.get("evidence_status"):
            errors.append(f"missing evidence_status: {edge.get('id')}")

    orphans = sorted(node_id for node_id in node_id_set if not degree[node_id])
    if orphans:
        errors.append(f"orphan nodes: {orphans[:10]}")

    concern_aliases: dict[str, str] = {}
    for node in nodes:
        if node.get("type") != "SkinConcern":
            continue
        if not node.get("aliases"):
            errors.append(f"concern has no aliases: {node.get('id')}")
        for alias in node.get("aliases") or []:
            key = "".join(character for character in str(alias).casefold() if character.isalnum())
            previous = concern_aliases.get(key)
            if key and previous and previous != node.get("id"):
                warnings.append(f"ambiguous concern alias {alias!r}: {previous}, {node.get('id')}")
            elif key:
                concern_aliases[key] = str(node.get("id"))

    required_aliases = {
        "酒糟肌": "concern_rosacea",
        "玫瑰痤瘡": "concern_rosacea",
        "痘痘": "concern_acne",
        "敏感肌": "concern_sensitive_skin",
    }
    for alias, expected in required_aliases.items():
        key = "".join(character for character in alias.casefold() if character.isalnum())
        if concern_aliases.get(key) != expected:
            errors.append(f"alias {alias!r} does not resolve to {expected}")

    return {
        "ok": not errors,
        "dataset_version": data.get("metadata", {}).get("version"),
        "nodes": len(nodes),
        "edges": len(edges),
        "node_types": dict(Counter(node.get("type") for node in nodes)),
        "edge_types": dict(Counter(edge.get("relation") for edge in edges)),
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    result = validate(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
