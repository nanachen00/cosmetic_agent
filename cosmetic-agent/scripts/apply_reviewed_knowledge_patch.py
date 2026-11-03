#!/usr/bin/env python3
"""Validate and idempotently apply a small reviewed BlinkKGv1 patch.

Dry-run is the default. Writes require --apply and an exact patch-id
acknowledgement. The patch stays inside the BlinkKGv1 namespace and every
public edge must carry approved evidence metadata.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
from typing import Any


ALLOWED_TYPES = {
    "SkinConcern": "SkinConcern",
    "Symptom": "Symptom",
    "Trigger": "Trigger",
    "CareNeed": "CareNeed",
    "SafetyWarning": "SafetyWarning",
    "Function": "Function",
    "Ingredient": "Ingredient",
    "DrugActiveIngredient": "DrugActive",
}
ALLOWED_RELATIONS = {
    "HAS_FUNCTION",
    "HAS_SYMPTOM",
    "HAS_TRIGGER",
    "RELEVANT_TO_CARE",
    "REQUIRES_CAUTION",
    "REQUIRES_FUNCTION",
    "SUPPORTS_NEED",
}
APPROVED_STATUSES = {"verified", "official-regulatory"}
SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def validate(data: dict[str, Any]) -> dict[str, Any]:
    patch_id = str(data.get("patch_id") or "").strip()
    reviewed_at = str(data.get("reviewed_at") or "").strip()
    if not patch_id:
        raise ValueError("patch_id is required")
    date.fromisoformat(reviewed_at)

    sources = list(data.get("sources") or [])
    nodes = list(data.get("nodes") or [])
    edges = list(data.get("edges") or [])
    source_ids = {str(item.get("id") or "") for item in sources}
    node_ids = {str(item.get("id") or "") for item in nodes}
    edge_ids = {str(item.get("id") or "") for item in edges}
    if "" in source_ids | node_ids | edge_ids:
        raise ValueError("all sources, nodes, and edges require stable ids")
    if len(source_ids) != len(sources) or len(node_ids) != len(nodes) or len(edge_ids) != len(edges):
        raise ValueError("duplicate source, node, or edge id")

    for source in sources:
        url = str(source.get("url") or "")
        if not url.startswith("https://"):
            raise ValueError(f"source {source['id']} requires an https URL")
    for node in nodes:
        if str(node.get("type") or "") not in ALLOWED_TYPES:
            raise ValueError(f"unsupported node type: {node.get('type')}")
        if not str(node.get("name") or "").strip():
            raise ValueError(f"node {node['id']} requires a name")
        refs = {str(value) for value in node.get("source_ids") or []}
        if not refs or refs - source_ids:
            raise ValueError(f"node {node['id']} has invalid source_ids")
    for edge in edges:
        relation = str(edge.get("relation") or "")
        if relation not in ALLOWED_RELATIONS or not SAFE_TOKEN.fullmatch(relation):
            raise ValueError(f"unsupported relation: {relation}")
        if str(edge.get("source") or "") not in node_ids or str(edge.get("target") or "") not in node_ids:
            raise ValueError(f"edge {edge['id']} references a missing node")
        if str(edge.get("evidence_status") or "") not in APPROVED_STATUSES:
            raise ValueError(f"edge {edge['id']} is not approved")
        refs = {str(value) for value in edge.get("source_ids") or []}
        if not refs or refs - source_ids:
            raise ValueError(f"edge {edge['id']} has invalid source_ids")
        for field in ("evidence_reference", "source_locator"):
            if not str(edge.get(field) or "").strip():
                raise ValueError(f"edge {edge['id']} requires {field}")
    return {
        "patch_id": patch_id,
        "reviewed_at": reviewed_at,
        "source_count": len(sources),
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def apply(data: dict[str, Any]) -> None:
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

    patch_id = str(data["patch_id"])
    reviewed_at = str(data["reviewed_at"])
    sources_by_id = {str(item["id"]): item for item in data["sources"]}
    graph = KnowledgeGraphRepository.from_env()
    try:
        with graph.session() as session:
            foreign = session.run(
                """
                MATCH (n:BlinkKGv1)
                WHERE n.id IN $ids
                  AND coalesce(n.knowledge_patch_id, '') <> $patch_id
                RETURN collect(n.id) AS ids
                """,
                ids=[str(node["id"]) for node in data["nodes"]],
                patch_id=patch_id,
            ).single()
            if foreign and foreign["ids"]:
                raise RuntimeError(f"refusing to overwrite existing nodes: {foreign['ids']}")

            for node_type, label in ALLOWED_TYPES.items():
                rows = []
                for node in data["nodes"]:
                    if node["type"] != node_type:
                        continue
                    props = dict(node)
                    props.update(
                        kg_id=node["id"],
                        entity_type=node_type,
                        display_name=node["name"],
                        normalized_name=str(node["name"]).casefold(),
                        review_status="verified",
                        reviewed_at=reviewed_at,
                        knowledge_patch_id=patch_id,
                        evidence_urls=[sources_by_id[value]["url"] for value in node["source_ids"]],
                        evidence_titles=[sources_by_id[value]["title"] for value in node["source_ids"]],
                    )
                    rows.append({"id": node["id"], "props": props})
                if not rows:
                    continue
                query = f"""
                UNWIND $rows AS row
                MERGE (n:BlinkKGv1:{label} {{id: row.id}})
                SET n += row.props
                """
                session.run(query, rows=rows).consume()

            for relation in sorted(ALLOWED_RELATIONS):
                rows = []
                for edge in data["edges"]:
                    if edge["relation"] != relation:
                        continue
                    props = dict(edge)
                    props.update(
                        kg_edge_id=edge["id"],
                        reviewed_at=reviewed_at,
                        knowledge_patch_id=patch_id,
                        evidence_urls=[sources_by_id[value]["url"] for value in edge["source_ids"]],
                        evidence_titles=[sources_by_id[value]["title"] for value in edge["source_ids"]],
                    )
                    rows.append(
                        {
                            "id": edge["id"],
                            "source": edge["source"],
                            "target": edge["target"],
                            "props": props,
                        }
                    )
                if not rows:
                    continue
                query = f"""
                UNWIND $rows AS row
                MATCH (s:BlinkKGv1 {{id: row.source}}), (t:BlinkKGv1 {{id: row.target}})
                MERGE (s)-[r:{relation} {{kg_edge_id: row.id}}]->(t)
                SET r += row.props
                """
                session.run(query, rows=rows).consume()
    finally:
        graph.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patch", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge-patch", default="")
    args = parser.parse_args()

    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    data = json.loads(args.patch.read_text(encoding="utf-8"))
    summary = validate(data)
    if args.apply:
        if args.acknowledge_patch != summary["patch_id"]:
            raise ValueError("--acknowledge-patch must exactly match patch_id")
        apply(data)
        summary["applied"] = True
    else:
        summary["applied"] = False
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
