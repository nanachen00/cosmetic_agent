"""Read-only knowledge-map queries over the reviewed seed overlay.

This repository deliberately does not know how to connect to Neo4j.  It uses
``migrate_knowledge_seed_v1.build_plan`` as the single publication/evidence
gate, then projects only the explicitly declared ``query_views``.  That makes
it useful as a safe runtime bridge while the isolated BlinkKGv1 database is
being prepared.
"""

from __future__ import annotations

import copy
import json
import unicodedata
from pathlib import Path
from typing import Any, Mapping

try:
    # Runtime location after the reviewed bundle is copied into the app.
    from scripts.migrate_knowledge_seed_v1 import MigrationPlan, PlanError, build_plan
except ImportError:  # pragma: no cover - local handoff-bundle layout
    from migrate_knowledge_seed_v1 import MigrationPlan, PlanError, build_plan

try:
    from app.services.knowledge_graph_view import RELATION_PRESENTATIONS
except ImportError:  # pragma: no cover - local handoff-bundle layout
    try:
        from knowledge_graph_view import RELATION_PRESENTATIONS
    except ImportError:  # pragma: no cover - copied without the optional view module
        RELATION_PRESENTATIONS: dict[str, dict[str, str]] = {}


def _normalize_exact(value: Any) -> str:
    """Normalize typography and whitespace without doing fuzzy resolution."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(text.split())


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [str(item).strip() for item in values if str(item).strip()]


class CuratedOverlayQueryRepository:
    """Produce the public KnowledgeGraph contract without writing any state."""

    def __init__(self, dataset: Mapping[str, Any], overlay: Mapping[str, Any]):
        self._dataset = copy.deepcopy(dict(dataset))
        self._overlay = copy.deepcopy(dict(overlay))
        self._plan = build_plan(self._dataset, overlay=self._overlay)
        self._nodes = self._index_nodes(self._plan)
        self._edges = self._index_edges(self._plan)
        self._sources = self._index_sources(self._plan)
        self._views_by_key: dict[str, dict[str, Any]] = {}
        self._views_by_center: dict[str, dict[str, Any]] = {}
        self._edge_ids_by_node: dict[str, list[str]] = {}
        self._index_views()

    @classmethod
    def from_paths(
        cls,
        seed_path: str | Path,
        overlay_path: str | Path,
    ) -> "CuratedOverlayQueryRepository":
        seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8"))
        return cls(seed, overlay)

    @property
    def plan_summary(self) -> dict[str, Any]:
        return copy.deepcopy(self._plan.summary)

    @staticmethod
    def _index_nodes(plan: MigrationPlan) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for label, rows in plan.nodes_by_label.items():
            if label == "EvidenceSource":
                continue
            for row in rows:
                properties = dict(row.get("props") or {})
                node_id = str(row.get("id") or properties.get("id") or "")
                if not node_id:
                    raise PlanError("planned public node is missing an id")
                result[node_id] = {
                    "id": node_id,
                    "type": str(
                        properties.get("type")
                        or properties.get("entity_type")
                        or label
                    ),
                    "name": str(
                        properties.get("name")
                        or properties.get("display_name")
                        or node_id
                    ),
                    "description": str(properties.get("description") or ""),
                    "properties": properties,
                }
        return result

    @staticmethod
    def _index_edges(plan: MigrationPlan) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for relation, rows in plan.edges_by_type.items():
            if relation == "SUPPORTED_BY":
                continue
            for row in rows:
                properties = dict(row.get("props") or {})
                edge_id = str(row.get("id") or properties.get("kg_edge_id") or "")
                if not edge_id:
                    raise PlanError("planned public edge is missing an id")
                result[edge_id] = {
                    "id": edge_id,
                    "source": str(row.get("source") or ""),
                    "target": str(row.get("target") or ""),
                    "relation": str(relation),
                    "properties": properties,
                }
        return result

    @staticmethod
    def _index_sources(plan: MigrationPlan) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in plan.nodes_by_label.get("EvidenceSource", []):
            properties = dict(row.get("props") or {})
            source_id = str(properties.get("source_id") or "")
            if source_id:
                result[source_id] = {
                    "id": source_id,
                    "title": str(
                        properties.get("title")
                        or properties.get("name")
                        or properties.get("display_name")
                        or source_id
                    ),
                    "url": properties.get("url"),
                    "review_status": properties.get("review_status"),
                    "properties": properties,
                }
        return result

    def _index_views(self) -> None:
        for raw_view in self._overlay.get("query_views") or []:
            view = copy.deepcopy(dict(raw_view))
            query_key = str(view.get("query_key") or "").strip()
            normalized = _normalize_exact(query_key)
            center_id = str(view.get("center_id") or "")
            if not normalized or not center_id:
                raise PlanError("every query_view requires query_key and center_id")
            if normalized in self._views_by_key:
                raise PlanError(f"duplicate normalized query_view key: {query_key!r}")
            center = self._nodes.get(center_id)
            if center is None:
                raise PlanError(
                    f"query_view {query_key!r} center did not pass the public plan gate"
                )
            declared_type = str(view.get("center_type") or "")
            if declared_type and declared_type != center["type"]:
                raise PlanError(
                    f"query_view {query_key!r} declares center type {declared_type!r}, "
                    f"planned node is {center['type']!r}"
                )
            ordered_edge_ids = [
                *[str(value) for value in view.get("edge_ids") or []],
                *[str(value) for value in view.get("drilldown_edge_ids") or []],
            ]
            missing = [edge_id for edge_id in ordered_edge_ids if edge_id not in self._edges]
            if missing:
                raise PlanError(
                    f"query_view {query_key!r} edges did not pass the public plan gate: {missing}"
                )
            view["_ordered_edge_ids"] = ordered_edge_ids
            self._views_by_key[normalized] = view
            if center_id in self._views_by_center:
                raise PlanError(f"multiple public query_views use center {center_id!r}")
            self._views_by_center[center_id] = view
            for edge_id in ordered_edge_ids:
                edge = self._edges[edge_id]
                for node_id in (str(edge["source"]), str(edge["target"])):
                    node_edge_ids = self._edge_ids_by_node.setdefault(node_id, [])
                    if edge_id not in node_edge_ids:
                        node_edge_ids.append(edge_id)

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """Return a curated center only for an exact, declared query key."""
        view = self._views_by_key.get(_normalize_exact(query))
        if view is None or int(limit) < 1:
            return []
        return [copy.deepcopy(self._nodes[str(view["center_id"])])]

    def explore(self, query: str, *, limit: int = 60) -> dict[str, Any] | None:
        view = self._views_by_key.get(_normalize_exact(query))
        if view is None:
            return None
        return self._project(view, query=str(query), limit=limit)

    def neighborhood(self, node_id: str, *, limit: int = 60) -> dict[str, Any] | None:
        normalized_node_id = str(node_id or "").strip()
        view = self._views_by_center.get(normalized_node_id)
        if view is None:
            edge_ids = self._edge_ids_by_node.get(normalized_node_id) or []
            center = self._nodes.get(normalized_node_id)
            if center is not None and edge_ids:
                view = {
                    "query_key": center["name"],
                    "center_id": normalized_node_id,
                    "center_type": center["type"],
                    "edge_ids": list(edge_ids),
                    "drilldown_edge_ids": [],
                    "initial_edge_limit_desktop": len(edge_ids),
                    "initial_edge_limit_mobile": min(2, len(edge_ids)),
                    "_ordered_edge_ids": list(edge_ids),
                }
        if view is None:
            return None
        return self._project(
            view,
            query=str(view["query_key"]),
            limit=limit,
            strategy="curated-overlay-node-id",
        )

    def _project(
        self,
        view: Mapping[str, Any],
        *,
        query: str,
        limit: int,
        strategy: str = "curated-overlay-exact",
    ) -> dict[str, Any]:
        center_id = str(view["center_id"])
        safe_limit = max(1, min(int(limit), 60))
        all_edge_ids = list(view["_ordered_edge_ids"])
        selected_edge_ids = all_edge_ids[:safe_limit]
        edge_ids_initial = [str(value) for value in view.get("edge_ids") or []]
        drilldown_ids = [str(value) for value in view.get("drilldown_edge_ids") or []]

        nodes: list[dict[str, Any]] = [copy.deepcopy(self._nodes[center_id])]
        seen_nodes = {center_id}
        edges: list[dict[str, Any]] = []
        source_ids: set[str] = set(_as_string_list(nodes[0]["properties"].get("source_ids")))
        reviewed_at: set[str] = set()
        evidence_statuses: set[str] = set()

        for edge_id in selected_edge_ids:
            raw = self._edges[edge_id]
            properties = copy.deepcopy(raw["properties"])
            source_id, target_id = raw["source"], raw["target"]
            relation = raw["relation"]
            presentation = RELATION_PRESENTATIONS.get(relation, {})
            label = str(properties.get("label") or presentation.get("label") or relation)
            status = str(properties.get("evidence_status") or "")
            refs = _as_string_list(properties.get("source_ids"))
            locator = str(properties.get("source_locator") or "")
            review_date = str(properties.get("reviewed_at") or "")
            evidence = {
                "status": status,
                "source_ids": refs,
                "reference": properties.get("evidence_reference"),
                "source_locator": locator,
                "reviewed_at": review_date,
            }
            edges.append({
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "relation_label": label,
                "label_zh": label,
                "direction": "source_to_target",
                "direction_from_center": (
                    "outgoing" if source_id == center_id
                    else "incoming" if target_id == center_id
                    else "connected"
                ),
                "evidence_status": status,
                "evidence_reference": properties.get("evidence_reference"),
                "source_ids": refs,
                "source_locator": locator,
                "reviewed_at": review_date,
                "evidence": evidence,
                "properties": properties,
            })
            source_ids.update(refs)
            if status:
                evidence_statuses.add(status)
            if review_date:
                reviewed_at.add(review_date)
            for node_id in (source_id, target_id):
                if node_id not in seen_nodes:
                    node = self._nodes.get(node_id)
                    if node is None:
                        raise PlanError(f"public edge {edge_id!r} lost endpoint {node_id!r}")
                    nodes.append(copy.deepcopy(node))
                    seen_nodes.add(node_id)
                    source_ids.update(_as_string_list(node["properties"].get("source_ids")))

        source_records = [
            copy.deepcopy(self._sources[source_id])
            for source_id in sorted(source_ids)
            if source_id in self._sources
        ]
        center = copy.deepcopy(self._nodes[center_id])
        query_key = str(view["query_key"])
        result = {
            "center": center,
            "nodes": nodes,
            "edges": edges,
            "resolution": {
                "query": str(query).strip(),
                "canonical_node_id": center_id,
                "matched_alias": (
                    query_key if _normalize_exact(query_key) != _normalize_exact(center["name"])
                    else None
                ),
                "strategy": strategy,
            },
            "evidence": {
                "gate": "migrate_knowledge_seed_v1.build_plan",
                "policy": "explicit-user-visible-and-approved-only",
                "approved_statuses": sorted(
                    str(value) for value in self._overlay.get("approved_evidence_statuses") or []
                ),
                "statuses": sorted(evidence_statuses),
                "source_ids": sorted(source_ids),
                "sources": source_records,
                "reviewed_at": sorted(reviewed_at),
                "all_edges_have_source_locator": all(
                    bool(edge["source_locator"]) for edge in edges
                ),
            },
            "view": {
                "query_key": query_key,
                "center_type": str(view.get("center_type") or center["type"]),
                "edge_ids": selected_edge_ids,
                "initial_edge_ids": edge_ids_initial,
                "drilldown_edge_ids": drilldown_ids,
                "initial_edge_limit_desktop": view.get("initial_edge_limit_desktop"),
                "initial_edge_limit_mobile": view.get("initial_edge_limit_mobile"),
                "has_more": len(selected_edge_ids) < len(all_edge_ids),
            },
        }
        return result


class OverlayFirstCuratedRepository:
    """Decorate the live repository with the reviewed read-only overlay.

    KnowledgeExplorerService already asks its repository for ``curated_*``
    results before using the seed/live fallbacks.  This adapter resolves those
    calls from the overlay first and touches the database-backed repository
    only when no exact curated view exists.  Other repository methods are
    delegated unchanged, keeping this isolated to knowledge-map wiring.
    """

    def __init__(
        self,
        overlay_repository: CuratedOverlayQueryRepository,
        fallback_repository: Any,
    ) -> None:
        self._overlay = overlay_repository
        self._fallback = fallback_repository

    @classmethod
    def from_paths(
        cls,
        fallback_repository: Any,
        seed_path: str | Path,
        overlay_path: str | Path,
    ) -> "OverlayFirstCuratedRepository":
        return cls(
            CuratedOverlayQueryRepository.from_paths(seed_path, overlay_path),
            fallback_repository,
        )

    def curated_search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        curated = self._overlay.search(query, limit=limit)
        if curated:
            return curated
        fallback = getattr(self._fallback, "curated_search", None)
        return fallback(query, limit=limit) if callable(fallback) else []

    def curated_explore(self, query: str, *, limit: int = 60) -> dict[str, Any] | None:
        curated = self._overlay.explore(query, limit=limit)
        if curated is not None:
            return curated
        fallback = getattr(self._fallback, "curated_explore", None)
        return fallback(query, limit=limit) if callable(fallback) else None

    def curated_neighborhood(
        self,
        node_id: str,
        *,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        curated = self._overlay.neighborhood(node_id, limit=limit)
        if curated is not None:
            return curated
        fallback = getattr(self._fallback, "curated_neighborhood", None)
        return fallback(node_id, limit=limit) if callable(fallback) else None

    def __getattr__(self, name: str) -> Any:
        """Leave legacy live-graph methods and lifecycle behavior unchanged."""
        return getattr(self._fallback, name)
