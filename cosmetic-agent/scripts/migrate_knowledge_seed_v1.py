#!/usr/bin/env python3
"""Plan or import the reviewed BLINK knowledge seed into an isolated Neo4j DB.

Dry-run is the default.  Writes require a dedicated ``KG_MIGRATION_NEO4J_*``
connection, an explicit target acknowledgement, and ``--apply``.  The importer
uses its own ``BlinkKGv1`` namespace so that it cannot merge with product,
account, or skin-record nodes already used by the application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


DEFAULT_SEED = Path(__file__).resolve().parents[1] / "data/knowledge_graph/v1/knowledge_seed.json"
DEFAULT_OVERLAY = Path(__file__).resolve().parents[1] / "data/knowledge_graph/v1/public_overlay.curated.json"
TYPE_TO_LABEL = {
    "SkinConcern": "SkinConcern",
    "ConcernCategory": "ConcernCategory",
    "Symptom": "Symptom",
    "Trigger": "Trigger",
    "CareNeed": "CareNeed",
    "Ingredient": "Ingredient",
    "DrugActiveIngredient": "DrugActive",
    "DrugProduct": "MedicationName",
    "AliasTerm": "Alias",
    "Manufacturer": "Manufacturer",
    "RiskRule": "SafetyWarning",
    "SafetyWarning": "SafetyWarning",
    "ApprovedIndication": "ApprovedIndication",
    "Function": "Function",
}
DESIRED_PUBLIC_LABELS = frozenset({
    "SkinConcern", "ConcernCategory", "Symptom", "CareNeed", "Ingredient", "IngredientCategory",
    "DrugActive", "MedicationName", "Alias", "Manufacturer", "SafetyWarning",
    "ApprovedIndication", "Function", "Trigger", "EvidenceSource",
})
SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationPlan:
    migration_id: str
    nodes_by_label: dict[str, list[dict[str, Any]]]
    edges_by_type: dict[str, list[dict[str, Any]]]
    summary: dict[str, Any]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _neo4j_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
        return [item for item in value if item is not None]
    return _stable_json(value)


def _flat_properties(data: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _neo4j_value(value) for key, value in data.items() if value is not None}


def _review_status(node: dict[str, Any]) -> str:
    properties = node.get("properties") or {}
    explicit = node.get("review_status") or properties.get("review_status")
    if explicit:
        return str(explicit)
    if node.get("identity_status") == "verified-node":
        return "verified"
    return "needs-review"


def _apply_node_overrides(
    nodes: list[dict[str, Any]], overrides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(node.get("id") or ""): node for node in nodes}
    seen: set[str] = set()
    allowed = {
        "id", "expected_seed_type", "public_type", "public_name_zh_tw",
        "aliases_add", "aliases_remove", "migration_note",
    }
    result = [dict(node) for node in nodes]
    result_by_id = {str(node["id"]): node for node in result}
    for override in overrides:
        if not isinstance(override, dict):
            raise PlanError("every existing_node_override must be an object")
        extra = set(override) - allowed
        if extra:
            raise PlanError(f"node override contains unsupported fields: {sorted(extra)}")
        node_id = str(override.get("id") or "")
        if not node_id or node_id in seen:
            raise PlanError(f"duplicate or missing node override id: {node_id!r}")
        seen.add(node_id)
        original = by_id.get(node_id)
        if original is None:
            raise PlanError(f"node override references missing seed node: {node_id}")
        expected = str(override.get("expected_seed_type") or "")
        actual = str(original.get("type") or "")
        if not expected or actual != expected:
            raise PlanError(
                f"node override {node_id} expected seed type {expected!r}, found {actual!r}"
            )
        public_type = str(override.get("public_type") or "")
        if public_type not in TYPE_TO_LABEL:
            raise PlanError(f"node override {node_id} has unsupported public_type: {public_type!r}")
        node = result_by_id[node_id]
        node["type"] = public_type
        if override.get("public_name_zh_tw"):
            node["name"] = str(override["public_name_zh_tw"])
        properties = dict(node.get("properties") or {})
        aliases = list(node.get("aliases") or properties.get("aliases") or [])
        remove = {str(value).strip().casefold() for value in override.get("aliases_remove") or []}
        aliases = [value for value in aliases if str(value).strip().casefold() not in remove]
        present = {str(value).strip().casefold() for value in aliases}
        for value in override.get("aliases_add") or []:
            if str(value).strip().casefold() not in present:
                aliases.append(value)
                present.add(str(value).strip().casefold())
        node["aliases"] = aliases
        properties["aliases"] = aliases
        node["properties"] = properties
    return result


def _apply_edge_overrides(
    edges: list[dict[str, Any]], overrides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(edge.get("id") or ""): edge for edge in edges}
    result = [dict(edge) for edge in edges]
    result_by_id = {str(edge["id"]): edge for edge in result}
    seen: set[str] = set()
    allowed = {
        "id", "expected_relation", "expected_source", "expected_target",
        "expected_evidence_status", "expected_source_ids", "source_ids",
        "evidence_reference", "source_locator", "reviewed_at", "label",
        "migration_note",
    }
    for override in overrides:
        if not isinstance(override, dict):
            raise PlanError("every existing_edge_override must be an object")
        extra = set(override) - allowed
        if extra:
            raise PlanError(f"edge override contains unsupported fields: {sorted(extra)}")
        edge_id = str(override.get("id") or "")
        if not edge_id or edge_id in seen:
            raise PlanError(f"duplicate or missing edge override id: {edge_id!r}")
        seen.add(edge_id)
        original = by_id.get(edge_id)
        if original is None:
            raise PlanError(f"edge override references missing seed edge: {edge_id}")
        expected = (
            str(override.get("expected_relation") or ""),
            str(override.get("expected_source") or ""),
            str(override.get("expected_target") or ""),
        )
        actual = (
            str(original.get("relation") or ""),
            str(original.get("source") or ""),
            str(original.get("target") or ""),
        )
        if any(not value for value in expected) or expected != actual:
            raise PlanError(f"edge override {edge_id} expected identity {expected!r}, found {actual!r}")
        if "expected_evidence_status" in override:
            expected_status = str(override.get("expected_evidence_status") or "")
            actual_status = str(original.get("evidence_status") or "")
            if not expected_status or expected_status != actual_status:
                raise PlanError(
                    f"edge override {edge_id} expected evidence status "
                    f"{expected_status!r}, found {actual_status!r}"
                )
        original_source_ids = [str(value) for value in original.get("source_ids") or []]
        if "expected_source_ids" in override:
            expected_source_ids = [str(value) for value in override.get("expected_source_ids") or []]
            if not expected_source_ids or expected_source_ids != original_source_ids:
                raise PlanError(
                    f"edge override {edge_id} expected source ids "
                    f"{expected_source_ids!r}, found {original_source_ids!r}"
                )
        if "source_ids" in override:
            override_source_ids = [str(value) for value in override.get("source_ids") or []]
            # Curated edge overrides may repeat the already-correct provenance,
            # but cannot silently replace a seed claim's evidence identity.
            if not override_source_ids or override_source_ids != original_source_ids:
                raise PlanError(
                    f"edge override {edge_id} cannot replace source ids "
                    f"{original_source_ids!r} with {override_source_ids!r}"
                )
        edge = result_by_id[edge_id]
        for field in ("source_ids", "evidence_reference", "source_locator", "reviewed_at", "label"):
            if field in override:
                edge[field] = override[field]
    return result


def _assert_identifier(value: str, *, field: str) -> str:
    if not SAFE_TOKEN.fullmatch(value):
        raise PlanError(f"unsafe {field}: {value!r}")
    return value


def build_plan(
    dataset: dict[str, Any],
    *,
    overlay: dict[str, Any],
    migration_id: str | None = None,
) -> MigrationPlan:
    metadata = dict(dataset.get("metadata") or {})
    seed_nodes = list(dataset.get("nodes") or [])
    seed_edges = list(dataset.get("edges") or [])
    additional_nodes = list(overlay.get("additional_nodes") or [])
    additional_edges = list(overlay.get("additional_edges") or [])
    if not all(isinstance(value, dict) for value in seed_nodes + additional_nodes):
        raise PlanError("every seed or overlay node must be an object")
    if not all(isinstance(value, dict) for value in seed_edges + additional_edges):
        raise PlanError("every seed or overlay edge must be an object")
    seed_nodes = _apply_node_overrides(seed_nodes, list(overlay.get("existing_node_overrides") or []))
    nodes = seed_nodes + additional_nodes
    seed_edges = _apply_edge_overrides(seed_edges, list(overlay.get("existing_edge_overrides") or []))
    edges = seed_edges + additional_edges
    seed_sources = list(dataset.get("sources") or [])
    additional_sources = list(overlay.get("additional_sources") or [])
    if not all(isinstance(value, dict) for value in seed_sources + additional_sources):
        raise PlanError("every seed or overlay evidence source must be an object")
    sources = seed_sources + additional_sources
    overlay_identity = str(overlay.get("overlay_id") or overlay.get("version") or "")
    if not overlay_identity:
        overlay_identity = hashlib.sha256(_stable_json(overlay).encode("utf-8")).hexdigest()[:16]
    migration_id = migration_id or (
        f"{metadata.get('dataset_id', 'blink-kg')}:{metadata.get('version', 'v1')}:{overlay_identity}"
    )

    node_ids = [str(node.get("id") or "") for node in nodes]
    edge_ids = [str(edge.get("id") or "") for edge in edges]
    if any(not value for value in node_ids + edge_ids):
        raise PlanError("every seed node and edge must have a stable id")
    if len(node_ids) != len(set(node_ids)) or len(edge_ids) != len(set(edge_ids)):
        raise PlanError("seed contains duplicate node or edge ids")

    known_nodes = set(node_ids)
    source_ids = {str(source.get("id") or "") for source in sources}
    if "" in source_ids:
        raise PlanError("every evidence source must have a stable id")
    if len(source_ids) != len(sources):
        raise PlanError("seed/overlay contains duplicate evidence source ids")

    # Overlay records are curated claims, not patches.  They cannot overwrite
    # seed records, and invalid hidden records must not be allowed to lie in
    # wait for a later visibility-list change.
    for node in nodes:
        node_id = str(node["id"])
        unknown_refs = sorted({str(value) for value in node.get("source_ids") or []} - source_ids)
        if unknown_refs:
            raise PlanError(f"node {node_id} references unknown sources: {unknown_refs}")
    for edge in edges:
        edge_id = str(edge["id"])
        start, end = str(edge.get("source") or ""), str(edge.get("target") or "")
        if start not in known_nodes or end not in known_nodes:
            raise PlanError(f"edge {edge_id} references a missing endpoint")
        unknown_refs = sorted({str(value) for value in edge.get("source_ids") or []} - source_ids)
        if unknown_refs:
            raise PlanError(f"edge {edge_id} references unknown sources: {unknown_refs}")

    allowed_relations = {_assert_identifier(str(value), field="relationship type") for value in overlay.get("allowed_relations") or []}
    approved_statuses = {str(value) for value in overlay.get("approved_evidence_statuses") or []}
    visible_centers = {str(value) for value in overlay.get("user_visible_center_ids") or []}
    visible_edge_ids = {str(value) for value in overlay.get("user_visible_edge_ids") or []}
    if not allowed_relations or not approved_statuses or not visible_edge_ids:
        raise PlanError("public overlay must declare relations, approved evidence, and explicit user_visible_edge_ids")
    if visible_centers - known_nodes:
        raise PlanError(f"overlay references missing centers: {sorted(visible_centers - known_nodes)}")
    if visible_edge_ids - set(edge_ids):
        raise PlanError(
            f"overlay references missing user-visible edges: {sorted(visible_edge_ids - set(edge_ids))}"
        )
    for view in overlay.get("query_views") or []:
        if not isinstance(view, dict):
            raise PlanError("every query_view must be an object")
        query_key = str(view.get("query_key") or "")
        center_id = str(view.get("center_id") or "")
        initial_edge_ids = [str(value) for value in view.get("edge_ids") or []]
        drilldown_edge_ids = [str(value) for value in view.get("drilldown_edge_ids") or []]
        view_edge_ids = initial_edge_ids + drilldown_edge_ids
        missing = sorted(set(view_edge_ids) - set(edge_ids))
        if center_id not in known_nodes:
            raise PlanError(f"query_view {query_key!r} references missing center: {center_id}")
        if missing:
            raise PlanError(f"query_view {query_key!r} references missing edges: {missing}")
        if set(view_edge_ids) - visible_edge_ids:
            raise PlanError(f"query_view {query_key!r} contains edges not approved as user-visible")
        if len(view_edge_ids) != len(set(view_edge_ids)):
            raise PlanError(f"query_view {query_key!r} contains duplicate edge ids")
        if int(view.get("visible_edge_count", len(view_edge_ids))) != len(view_edge_ids):
            raise PlanError(f"query_view {query_key!r} visible_edge_count is inconsistent")
        view_nodes = {center_id}
        for edge_id in view_edge_ids:
            edge = edges[edge_ids.index(edge_id)]
            view_nodes.update((str(edge["source"]), str(edge["target"])))
        if int(view.get("visible_node_count", len(view_nodes))) != len(view_nodes):
            raise PlanError(f"query_view {query_key!r} visible_node_count is inconsistent")
        initial_nodes = {center_id}
        for edge_id in initial_edge_ids:
            edge = edges[edge_ids.index(edge_id)]
            initial_nodes.update((str(edge["source"]), str(edge["target"])))
        if int(view.get("initial_visible_edge_count", len(initial_edge_ids))) != len(initial_edge_ids):
            raise PlanError(f"query_view {query_key!r} initial_visible_edge_count is inconsistent")
        if int(view.get("initial_visible_node_count", len(initial_nodes))) != len(initial_nodes):
            raise PlanError(f"query_view {query_key!r} initial_visible_node_count is inconsistent")

    public_edges: list[dict[str, Any]] = []
    skipped_statuses: Counter[str] = Counter()
    skipped_reasons: Counter[str] = Counter()
    for edge in edges:
        edge_id = str(edge["id"])
        relation = str(edge.get("relation") or "")
        status = str(edge.get("evidence_status") or "")
        start, end = str(edge.get("source") or ""), str(edge.get("target") or "")
        visible = edge_id in visible_edge_ids
        if not visible:
            skipped_reasons["not-user-visible"] += 1
            continue
        if relation not in allowed_relations:
            skipped_reasons["relation-not-allowed"] += 1
            continue
        if status not in approved_statuses:
            skipped_reasons["evidence-not-approved"] += 1
            skipped_statuses[status or "missing"] += 1
            continue
        source_node, target_node = next((n for n in nodes if str(n["id"]) == start), None), next((n for n in nodes if str(n["id"]) == end), None)
        if not source_node or not target_node:
            raise PlanError(f"edge {edge_id} references a missing endpoint")
        if str(source_node.get("type")) not in TYPE_TO_LABEL or str(target_node.get("type")) not in TYPE_TO_LABEL:
            skipped_reasons["endpoint-type-not-public"] += 1
            continue
        public_edges.append(edge)

    selected_node_ids = set(visible_centers)
    for edge in public_edges:
        selected_node_ids.update((str(edge["source"]), str(edge["target"])))

    nodes_by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_source_nodes: list[str] = []
    unsupported_types: Counter[str] = Counter()
    for node in nodes:
        node_id = str(node["id"])
        label = TYPE_TO_LABEL.get(str(node.get("type") or ""))
        if label is None:
            unsupported_types[str(node.get("type") or "UNKNOWN")] += 1
            continue
        if node_id not in selected_node_ids:
            continue
        refs = [str(value) for value in node.get("source_ids") or []]
        if not refs:
            missing_source_nodes.append(node_id)
        unknown_refs = sorted(set(refs) - source_ids)
        if unknown_refs:
            raise PlanError(f"node {node_id} references unknown sources: {unknown_refs}")
        node_properties = dict(node.get("properties") or {})
        aliases = list(node.get("aliases") or node_properties.get("aliases") or [])
        props = _flat_properties({
            **node_properties,
            "kg_id": node_id,
            "id": node_id,
            "entity_type": node.get("type"),
            "type": node.get("type"),
            "display_name": node.get("name"),
            "name": node.get("name"),
            "normalized_name": node.get("normalized_name"),
            "aliases": aliases,
            "source_ids": refs,
            "review_status": _review_status(node),
        })
        nodes_by_label[label].append({"id": node_id, "props": props})

    selected_source_ids = {
        str(ref)
        for node in nodes if str(node.get("id")) in selected_node_ids
        for ref in node.get("source_ids") or []
    }
    selected_source_ids.update(str(ref) for edge in public_edges for ref in edge.get("source_ids") or [])
    for source in sources:
        source_id = str(source["id"])
        if source_id not in selected_source_ids:
            continue
        props = _flat_properties({
            **source,
            "kg_id": f"evidence_source:{source_id}",
            "source_id": source_id,
            "display_name": source.get("title") or source.get("name") or source_id,
            "review_status": source.get("review_status") or "source-linked",
        })
        nodes_by_label["EvidenceSource"].append({"id": props["kg_id"], "props": props})

    edges_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_edge_source: list[str] = []
    missing_edge_review: list[str] = []
    for edge in public_edges:
        edge_id = str(edge["id"])
        start, end = str(edge.get("source") or ""), str(edge.get("target") or "")
        if start not in known_nodes or end not in known_nodes:
            raise PlanError(f"edge {edge_id} references a missing endpoint")
        relation = _assert_identifier(str(edge.get("relation") or ""), field="relationship type")
        refs = [str(value) for value in edge.get("source_ids") or []]
        status = str(edge.get("evidence_status") or "")
        if not refs:
            missing_edge_source.append(edge_id)
        if not status:
            missing_edge_review.append(edge_id)
        if not str(edge.get("source_locator") or "").strip():
            raise PlanError(f"public edge {edge_id} is missing source_locator")
        if not str(edge.get("reviewed_at") or "").strip():
            raise PlanError(f"public edge {edge_id} is missing reviewed_at")
        unknown_refs = sorted(set(refs) - source_ids)
        if unknown_refs:
            raise PlanError(f"edge {edge_id} references unknown sources: {unknown_refs}")
        props = _flat_properties({
            **dict(edge.get("properties") or {}),
            "kg_edge_id": edge_id,
            "source_ids": refs,
            "evidence_status": status,
            "evidence_reference": edge.get("evidence_reference"),
            "source_locator": edge.get("source_locator"),
            "reviewed_at": edge.get("reviewed_at"),
            "label": edge.get("label"),
        })
        edges_by_type[relation].append({"id": edge_id, "source": start, "target": end, "props": props})

    for node in nodes:
        if str(node.get("id")) not in selected_node_ids or str(node.get("type") or "") not in TYPE_TO_LABEL:
            continue
        for ref in node.get("source_ids") or []:
            edge_id = f"source:{node['id']}:{ref}"
            edges_by_type["SUPPORTED_BY"].append({
                "id": edge_id,
                "source": str(node["id"]),
                "target": f"evidence_source:{ref}",
                "props": {"kg_edge_id": edge_id, "source_ids": [str(ref)], "evidence_status": "source-linked"},
            })

    present_labels = set(nodes_by_label)
    summary = {
        "mode": "dry-run",
        "seed_nodes": len(seed_nodes),
        "seed_edges": len(seed_edges),
        "seed_sources": len(seed_sources),
        "overlay_additional_nodes": len(additional_nodes),
        "overlay_additional_edges": len(additional_edges),
        "overlay_additional_sources": len(additional_sources),
        "evidence_sources": len(sources),
        "planned_nodes": sum(map(len, nodes_by_label.values())),
        "planned_edges": sum(map(len, edges_by_type.values())),
        "node_labels": {key: len(value) for key, value in sorted(nodes_by_label.items())},
        "relationship_types": {key: len(value) for key, value in sorted(edges_by_type.items())},
        "missing_public_labels": sorted(DESIRED_PUBLIC_LABELS - present_labels),
        "unsupported_seed_types": dict(sorted(unsupported_types.items())),
        "missing_source_nodes": missing_source_nodes,
        "missing_source_edges": missing_edge_source,
        "missing_review_edges": missing_edge_review,
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
        "skipped_evidence_statuses": dict(sorted(skipped_statuses.items())),
        "overlay_version": overlay.get("version"),
        "rollback_manifest": {
            "migration_id": migration_id,
            "namespace_label": "BlinkKGv1",
            "node_count": sum(map(len, nodes_by_label.values())),
            "edge_count": sum(map(len, edges_by_type.values())),
        },
    }
    if missing_source_nodes or missing_edge_source or missing_edge_review:
        raise PlanError("source/evidence/review fields are incomplete; refusing to build an import plan")
    return MigrationPlan(migration_id, dict(nodes_by_label), dict(edges_by_type), summary)


def target_identity(uri: str, database: str) -> str:
    parsed = urlsplit(uri)
    return f"{parsed.hostname or ''}/{database}".lower()


def assert_write_target(
    *, uri: str, database: str, acknowledgement: str,
    production_uri: str | None, production_database: str | None,
) -> None:
    identity = target_identity(uri, database)
    if acknowledgement.lower() != identity:
        raise PlanError(f"target acknowledgement must exactly equal {identity!r}")
    if not production_uri or not production_database:
        raise PlanError("production Neo4j identity is required before any write")
    if target_identity(production_uri, production_database) == identity:
        raise PlanError("migration target matches the production Neo4j host/database")


def _chunks(rows: list[dict[str, Any]], size: int = 500) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def apply_plan(driver: Any, *, database: str, plan: MigrationPlan) -> None:
    with driver.session(database=database) as session:
        session.execute_write(lambda tx: tx.run(
            "CREATE CONSTRAINT blink_kg_v1_id IF NOT EXISTS FOR (n:BlinkKGv1) REQUIRE n.kg_id IS UNIQUE"
        ).consume())
        for label, rows in plan.nodes_by_label.items():
            _assert_identifier(label, field="label")
            cypher = f"""
            UNWIND $rows AS row
            MERGE (n:BlinkKGv1:{label} {{kg_id: row.id}})
            SET n += row.props, n.kg_migration_id = $migration_id
            """
            for batch in _chunks(rows):
                session.execute_write(lambda tx, q=cypher, b=batch: tx.run(q, rows=b, migration_id=plan.migration_id).consume())
        for relation, rows in plan.edges_by_type.items():
            _assert_identifier(relation, field="relationship type")
            cypher = f"""
            UNWIND $rows AS row
            MATCH (s:BlinkKGv1 {{kg_id: row.source}}), (t:BlinkKGv1 {{kg_id: row.target}})
            MERGE (s)-[r:{relation} {{kg_edge_id: row.id}}]->(t)
            SET r += row.props, r.kg_migration_id = $migration_id
            """
            for batch in _chunks(rows):
                session.execute_write(lambda tx, q=cypher, b=batch: tx.run(q, rows=b, migration_id=plan.migration_id).consume())


def assert_namespace_compatible(driver: Any, *, database: str, migration_id: str) -> None:
    """Refuse to merge a new migration over another BlinkKGv1 snapshot."""
    with driver.session(database=database) as session:
        record = session.execute_read(lambda tx: tx.run(
            """
            MATCH (n:BlinkKGv1)
            WHERE coalesce(n.kg_migration_id, '') <> $id
            RETURN count(n) AS foreign_count
            """,
            id=migration_id,
        ).single())
    if record and int(record.get("foreign_count", 0)):
        raise PlanError("BlinkKGv1 namespace contains data from a different migration")



def rollback(driver: Any, *, database: str, migration_id: str) -> None:
    with driver.session(database=database) as session:
        session.execute_write(lambda tx: tx.run(
            "MATCH ()-[r]->() WHERE r.kg_migration_id = $id DELETE r", id=migration_id
        ).consume())
        session.execute_write(lambda tx: tx.run(
            "MATCH (n:BlinkKGv1) WHERE n.kg_migration_id = $id DETACH DELETE n", id=migration_id
        ).consume())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--migration-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--database", default=os.getenv("KG_MIGRATION_NEO4J_DATABASE", ""))
    parser.add_argument("--acknowledge-target", default="")
    parser.add_argument("--production-uri", default=os.getenv("PRODUCTION_NEO4J_URI") or os.getenv("NEO4J_URI"))
    parser.add_argument("--production-database", default=os.getenv("PRODUCTION_NEO4J_DATABASE") or os.getenv("NEO4J_DATABASE"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dataset = json.loads(args.seed.read_text(encoding="utf-8"))
    overlay = json.loads(args.overlay.read_text(encoding="utf-8"))
    plan = build_plan(dataset, overlay=overlay, migration_id=args.migration_id)
    if not args.apply and not args.rollback:
        print(_stable_json({"migration_id": plan.migration_id, **plan.summary}))
        return 0

    uri = os.getenv("KG_MIGRATION_NEO4J_URI", "")
    user = os.getenv("KG_MIGRATION_NEO4J_USER", "")
    password = os.getenv("KG_MIGRATION_NEO4J_PASSWORD", "")
    if not uri or not user or not password or not args.database:
        raise PlanError("writes require dedicated KG_MIGRATION_NEO4J_URI/USER/PASSWORD/DATABASE")
    assert_write_target(
        uri=uri,
        database=args.database,
        acknowledgement=args.acknowledge_target,
        production_uri=args.production_uri,
        production_database=args.production_database,
    )
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        if args.rollback:
            rollback(driver, database=args.database, migration_id=plan.migration_id)
        else:
            assert_namespace_compatible(driver, database=args.database, migration_id=plan.migration_id)
            apply_plan(driver, database=args.database, plan=plan)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
