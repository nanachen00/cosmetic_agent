"""Read-only, versioned seed graph used before Neo4j migration is approved."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "knowledge_graph"
    / "v1"
    / "knowledge_seed.json"
)


def normalize_lookup(value: str) -> str:
    """Normalize Chinese/English aliases without translating or guessing meaning."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())[:160]


@lru_cache(maxsize=4)
def _load_seed(path: str) -> dict[str, Any]:
    seed_path = Path(path)
    if not seed_path.is_file():
        # The reviewed cloud graph is the primary source in production.  A
        # missing local overlay must not disable live read-only exploration.
        return {"metadata": {}, "nodes": [], "edges": []}
    with seed_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _serialize_node(node: dict[str, Any], *, matched_alias: str | None = None) -> dict[str, Any]:
    reserved = {"id", "type", "name", "description"}
    result = {
        "id": str(node["id"]),
        "type": str(node.get("type") or "Knowledge"),
        "name": str(node.get("name") or node["id"])[:300],
        "description": str(node.get("description") or "")[:2_000],
        "properties": {
            key: value for key, value in node.items() if key not in reserved
        },
        "dataset_version": "1.0.0-draft",
    }
    if matched_alias:
        result["matched_alias"] = matched_alias
    return result


class KnowledgeSeedRepository:
    """Search and traverse the draft graph without mutating the live database."""

    def __init__(self, path: Path | str = DEFAULT_SEED_PATH) -> None:
        self.path = Path(path)
        self.data = _load_seed(str(self.path.resolve()))
        self.nodes = {str(node["id"]): node for node in self.data.get("nodes", [])}
        self.edges = list(self.data.get("edges", []))
        self._aliases: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in self.nodes.values():
            values = [
                node.get("name"),
                node.get("name_en"),
                node.get("normalized_name"),
                *(node.get("aliases") or []),
            ]
            seen: set[str] = set()
            for value in values:
                text = str(value or "").strip()
                key = normalize_lookup(text)
                if key and key not in seen:
                    self._aliases[key].append((str(node["id"]), text))
                    seen.add(key)
        for edge in self.edges:
            self._adjacency[str(edge["source"])].append(edge)
            self._adjacency[str(edge["target"])].append(edge)

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            counts[str(node.get("type") or "Knowledge")] += 1
        return {
            "dataset_id": self.data.get("metadata", {}).get("dataset_id"),
            "dataset_version": self.data.get("metadata", {}).get("version"),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(counts),
            "mode": (
                "read-only-seed-overlay" if self.path.is_file() else "cloud-live-only"
            ),
            "available": self.path.is_file(),
        }

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        query_text = " ".join(str(query or "").split())[:160]
        query_key = normalize_lookup(query_text)
        if not query_key:
            return []
        scored: dict[str, tuple[int, int, str]] = {}
        for alias_key, matches in self._aliases.items():
            if alias_key == query_key:
                score = 0
            elif alias_key in query_key or query_key in alias_key:
                score = 1
            else:
                english_tokens = set(re.findall(r"[a-z0-9]+", query_text.casefold()))
                alias_tokens = set(re.findall(r"[a-z0-9]+", alias_key))
                overlap = len(english_tokens & alias_tokens)
                if not overlap:
                    continue
                score = 3
            for node_id, matched_alias in matches:
                candidate = (score, len(alias_key), matched_alias)
                if node_id not in scored or candidate < scored[node_id]:
                    scored[node_id] = candidate
        ordered = sorted(
            scored.items(),
            key=lambda item: (
                item[1][0],
                item[1][1],
                str(self.nodes[item[0]].get("name") or "").casefold(),
            ),
        )
        safe_limit = max(1, min(int(limit), 30))
        results: list[dict[str, Any]] = []
        result_ids: set[str] = set()
        for node_id, score in ordered:
            canonical_id = node_id
            if self.nodes[node_id].get("type") == "AliasTerm":
                for edge in self._adjacency.get(node_id, []):
                    if edge.get("relation") != "ALIAS_OF":
                        continue
                    canonical_id = (
                        str(edge["target"])
                        if str(edge["source"]) == node_id
                        else str(edge["source"])
                    )
                    break
            if canonical_id in result_ids:
                continue
            results.append(
                _serialize_node(self.nodes[canonical_id], matched_alias=score[2])
            )
            result_ids.add(canonical_id)
            if len(results) >= safe_limit:
                break
        return results

    def neighborhood(
        self,
        node_id: str,
        *,
        depth: int = 2,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        center_id = str(node_id or "").strip()
        if center_id not in self.nodes:
            return None
        safe_depth = max(1, min(int(depth), 2))
        safe_limit = max(1, min(int(limit), 120))
        distances = {center_id: 0}
        queue: deque[str] = deque([center_id])
        selected_edges: list[dict[str, Any]] = []
        selected_edge_ids: set[str] = set()
        while queue and len(selected_edges) < safe_limit:
            current = queue.popleft()
            if distances[current] >= safe_depth:
                continue
            for edge in self._adjacency.get(current, []):
                edge_id = str(edge["id"])
                if edge_id not in selected_edge_ids:
                    selected_edges.append(dict(edge))
                    selected_edge_ids.add(edge_id)
                    if len(selected_edges) >= safe_limit:
                        break
                neighbor = (
                    str(edge["target"])
                    if str(edge["source"]) == current
                    else str(edge["source"])
                )
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
        ordered_ids = [center_id] + sorted(
            (node_id for node_id in distances if node_id != center_id),
            key=lambda item: (distances[item], str(self.nodes[item].get("name") or "")),
        )
        visualization = self.data.get("metadata", {}).get("visualization", {})
        group_config = visualization.get("groups", {})
        group_labels = {
            "aliases": "常見叫法",
            "ambiguity": "可能指的是",
            "classification": "分類",
            "products": "含此有效成分的藥品",
            "manufacturers": "製造廠／許可資料",
            "approved_indications": "TFDA 核准適應症",
            "concerns": "對應問題／症狀",
            "ingredients": "對應成分",
            "functions": "成分類型／功能",
            "drug_classes": "藥品分類",
            "risk": "風險與注意",
            "other": "其他關係",
        }
        relation_groups = {
            relation: group
            for group, relations in group_config.items()
            for relation in relations
        }
        grouped: dict[str, dict[str, Any]] = {}
        for edge in selected_edges:
            if center_id not in {str(edge["source"]), str(edge["target"])}:
                continue
            group = relation_groups.get(str(edge.get("relation")), "other")
            item = grouped.setdefault(
                group,
                {
                    "id": group,
                    "label": group_labels.get(group, group),
                    "edge_count": 0,
                    "preview_node_ids": [],
                    "collapsed": True,
                },
            )
            item["edge_count"] += 1
            neighbor = (
                str(edge["target"])
                if str(edge["source"]) == center_id
                else str(edge["source"])
            )
            if neighbor not in item["preview_node_ids"] and len(item["preview_node_ids"]) < 5:
                item["preview_node_ids"].append(neighbor)
        ordered_groups = [
            grouped[group]
            for group in [*group_config, "other"]
            if group in grouped
        ]
        return {
            "center": _serialize_node(self.nodes[center_id]),
            "nodes": [_serialize_node(self.nodes[item]) for item in ordered_ids],
            "edges": selected_edges,
            "groups": ordered_groups,
            "depth": safe_depth,
            "dataset": self.status(),
        }

    def explore(
        self,
        query: str,
        *,
        depth: int = 2,
        limit: int = 60,
    ) -> dict[str, Any] | None:
        matches = self.search(query, limit=1)
        if not matches:
            return None
        graph = self.neighborhood(matches[0]["id"], depth=depth, limit=limit)
        if graph is not None:
            graph["resolution"] = {
                "query": str(query).strip(),
                "canonical_node_id": matches[0]["id"],
                "matched_alias": matches[0].get("matched_alias"),
                "strategy": "normalized-alias",
            }
        return graph
