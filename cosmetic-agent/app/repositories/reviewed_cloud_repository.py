"""Read-only cloud authority for the knowledge explorer, never the chat agent.

Seed/overlay files are migration inputs, not a production fallback. The wrapped
repository already restricts curated reads to the BlinkKGv1 namespace. Keep that
boundary for every lookup, including results found by the legacy vector index.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import unicodedata
from typing import Any
from app.services.cloud_semantic_catalog import CloudSemanticCatalog


APPROVED_STATUSES = frozenset({"verified", "official-regulatory"})
PUBLIC_RELATIONS = frozenset({
    "HAS_FUNCTION", "HAS_SYMPTOM", "HAS_TRIGGER", "MAY_REFER_TO",
    "RELEVANT_TO_CARE", "REQUIRES_CAUTION", "REQUIRES_FUNCTION", "SUPPORTS_NEED",
})


def lookup_key(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def is_exact(query: str, node: dict[str, Any]) -> bool:
    props = node.get("properties") or {}
    aliases = props.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    values = [node.get("id"), node.get("name"), props.get("name"),
              props.get("display_name"), props.get("normalized_name"), *aliases]
    key = lookup_key(query)
    return bool(key) and any(lookup_key(value) == key for value in values if value)


def reviewed_graph(graph: dict[str, Any] | None) -> dict[str, Any] | None:
    if graph is None:
        return None
    result = deepcopy(graph)
    center_id = str(result["center"]["id"])
    nodes = {str(node["id"]): node for node in result.get("nodes") or []}
    nodes[center_id] = result["center"]
    selected = []
    seen = set()
    for edge in result.get("edges") or []:
        source = str(edge.get("source") or edge.get("source_id") or "")
        target = str(edge.get("target") or edge.get("target_id") or "")
        sources = edge.get("source_ids")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(value, str) and value.strip() for value in sources
        ):
            continue
        try:
            date.fromisoformat(str(edge.get("reviewed_at") or ""))
        except ValueError:
            continue
        if (
            edge.get("relation") not in PUBLIC_RELATIONS
            or edge.get("evidence_status") not in APPROVED_STATUSES
            or not str(edge.get("source_locator") or "").strip()
            or not str(edge.get("evidence_reference") or "").strip()
            or not str(edge.get("id") or "").strip()
            or source not in nodes or target not in nodes
            or center_id not in (source, target)
        ):
            continue
        identity = (source, str(edge["relation"]), target)
        if identity in seen:
            continue
        seen.add(identity)
        edge.update(source=source, target=target, source_id=source, target_id=target)
        selected.append(edge)
    used = {center_id}
    for edge in selected:
        used.update((edge["source"], edge["target"]))
    result["nodes"] = [node for key, node in nodes.items() if key in used]
    result["edges"] = selected
    result["data_source"] = {
        "backend": "neo4j", "namespace": "BlinkKGv1",
        "policy": "reviewed-cloud-only", "seed_required": False,
        "llm_generated_relations": False,
    }
    return result


class NoRuntimeSeedRepository:
    """Compatibility port: production cannot fall back to a missing/stale file."""

    def status(self) -> dict[str, Any]:
        return {"enabled": False, "runtime_required": False,
                "reason": "cloud_is_authoritative"}

    def search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def neighborhood(self, *args: Any, **kwargs: Any) -> None:
        return None

    def explore(self, *args: Any, **kwargs: Any) -> None:
        return None


class ReviewedCloudRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository
        self._semantic_catalog = None

    def configure_semantic(self, embedder: Any) -> None:
        self._semantic_catalog = CloudSemanticCatalog(self._repository.curated_catalog, embedder)

    def close(self) -> None:
        self._repository.close()

    def curated_search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        # Retry compact spelling for "A 酸" without inventing a new alias/node.
        spelling = " ".join(str(query).split()).casefold()
        queries = list(dict.fromkeys([spelling, lookup_key(query)]))
        found = {}
        for spelling in queries:
            if not spelling:
                continue
            for node in self._repository.curated_search(spelling, limit=30):
                found.setdefault(str(node["id"]), node)
        ranked = sorted(found.values(), key=lambda n: (
            not is_exact(query, n), str(n.get("name") or ""), str(n["id"]),
        ))
        return ranked[:max(1, min(int(limit), 30))]

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        return self.curated_search(query, limit=limit)

    def curated_neighborhood(self, node_id: str, *, limit: int = 30) -> dict[str, Any] | None:
        return reviewed_graph(self._repository.curated_neighborhood(node_id, limit=limit))

    def neighborhood(self, node_id: str, *, limit: int = 30) -> dict[str, Any] | None:
        return self.curated_neighborhood(node_id, limit=limit)

    def curated_explore(self, query: str, *, limit: int = 30) -> dict[str, Any] | None:
        matches = self.curated_search(query, limit=30)
        exact = [node for node in matches if is_exact(query, node)]
        if not exact:
            return None
        graph = self.curated_neighborhood(exact[0]["id"], limit=limit)
        if graph is not None:
            graph["resolution"] = {"query": query, "canonical_node_id": exact[0]["id"],
                                   "strategy": "reviewed-cloud-exact"}
        return graph

    def semantic_search(self, vector: list[float], *, limit: int = 12) -> list[dict[str, Any]]:
        if self._semantic_catalog is not None:
            return self._semantic_catalog.search(vector, limit=limit)
        # Compatibility for explicitly injected repositories without a cloud catalog.
        # Normal application pipelines always configure the cloud-only catalog.
        # Vector similarity proposes an entity, never establishes a relationship.
        result = []
        seen = set()
        for candidate in self._repository.semantic_search(vector, limit=limit):
            name = str(candidate.get("name") or "")
            for node in self.curated_search(name, limit=30):
                if not is_exact(name, node) or node["id"] in seen:
                    continue
                seen.add(node["id"])
                merged = dict(node)
                for key in ("score", "similarity", "semantic_score"):
                    if key in candidate:
                        merged[key] = candidate[key]
                merged["semantic_source_id"] = str(candidate.get("id") or "")
                result.append(merged)
                if len(result) >= max(1, min(int(limit), 30)):
                    return result
        return result
