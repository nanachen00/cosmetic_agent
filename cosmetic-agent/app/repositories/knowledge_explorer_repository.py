"""Read-only projection of approved Neo4j knowledge nodes for the explorer UI."""

from __future__ import annotations

import os
from typing import Any

from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository


ALLOWED_LABELS = (
    "Ingredient",
    "SkinConcern",
    "RiskRule",
    "Regulation",
    "Product",
    "Category",
)

PUBLIC_RELATIONS = {
    "CONFLICTS_WITH",
    "CONTAINS",
    "REGULATES_CATEGORY",
}

REVIEW_REQUIRED_RELATIONS = {
    "APPLIES_TO_RULE",
    "WARNS_CONCERN",
    "SUITABLE_FOR_CONCERN",
}

RELATION_LABELS = {
    "CONFLICTS_WITH": "不建議同時使用",
    "CONTAINS": "含有成分",
    "REGULATES_CATEGORY": "規範品類",
    "APPLIES_TO_RULE": "適用風險規則",
    "WARNS_CONCERN": "可能影響",
    "SUITABLE_FOR_CONCERN": "可能適合",
}


CURATED_RELATION_LABELS = {
    "HAS_FUNCTION": "主要作用",
    "HAS_SYMPTOM": "常見症狀",
    "HAS_TRIGGER": "常見誘因",
    "MAY_REFER_TO": "可能指的是",
    "RELEVANT_TO_CARE": "照護方向",
    "REQUIRES_CAUTION": "使用／判讀注意",
    "REQUIRES_FUNCTION": "需要的功能",
    "SUPPORTS_NEED": "可支援需求",
}
CURATED_RELATIONS = tuple(CURATED_RELATION_LABELS)


def include_unreviewed_relations() -> bool:
    return os.getenv("KNOWLEDGE_GRAPH_INCLUDE_UNREVIEWED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def classify_edge_evidence(
    relation: str,
    relation_properties: dict[str, Any],
    source_properties: dict[str, Any],
) -> tuple[str, str]:
    """Return a public evidence tier and a short, non-secret source reference."""
    if relation == "CONFLICTS_WITH":
        source = str(relation_properties.get("source") or "").strip()
        documented = bool(
            source.startswith(("https://", "http://"))
            and (
                relation_properties.get("description_zh")
                or relation_properties.get("description_en")
            )
        )
        return ("documented" if documented else "needs-review", source[:300])
    if relation == "CONTAINS":
        reference = str(
            source_properties.get("source_url")
            or source_properties.get("source")
            or ""
        ).strip()
        return ("source-linked" if reference else "needs-review", reference[:500])
    if relation == "REGULATES_CATEGORY":
        reference = str(
            source_properties.get("law_name")
            or source_properties.get("text")
            or ""
        ).strip()
        return ("source-linked" if reference else "needs-review", reference[:500])
    return ("needs-review", "")

DISPLAY_KEYS = {
    "Ingredient": ("name_zh", "name_en", "normalized_name", "mongo_id"),
    "SkinConcern": ("name", "name_en", "normalized_name", "mongo_id"),
    "RiskRule": ("risk_label", "description", "mongo_id"),
    "Regulation": ("law_name", "article", "text", "mongo_id"),
    "Product": ("name", "sku"),
    "Category": ("name",),
}

DESCRIPTION_KEYS = {
    "Ingredient": ("benefits", "usage_notes", "risks", "contraindications"),
    "SkinConcern": ("description", "common_signs"),
    "RiskRule": ("description", "recommendation", "risk_outcomes"),
    "Regulation": ("text",),
    "Product": ("category", "brand"),
    "Category": (),
}

SAFE_PROPERTY_KEYS = {
    "Ingredient": {
        "aliases", "avoid_for", "benefits", "contraindications",
        "ingredient_type", "last_verified_at", "mongo_id", "name_en",
        "name_zh", "normalized_name", "risks", "source_types", "status",
        "suitable_for", "usage_notes",
    },
    "SkinConcern": {
        "category", "common_signs", "description", "last_verified_at",
        "mongo_id", "name", "name_en", "normalized_name",
        "related_concern_ids", "status",
    },
    "RiskRule": {
        "conditions", "description", "mongo_id", "priority",
        "recommendation", "risk_label", "risk_level", "risk_outcomes",
        "rule_category", "status",
    },
    "Regulation": {
        "article", "law_name", "law_type", "mongo_id", "text", "type",
    },
    "Product": {
        "brand", "category", "is_partner", "name", "price", "priority",
        "sku", "source", "source_type", "source_url",
    },
    "Category": {"name"},
}


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in list(value)[:30]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _safe_value(item)
            for key, item in list(value.items())[:30]
        }
    return str(value)[:2_000]


def _primary_label(labels: list[str]) -> str:
    return next((label for label in ALLOWED_LABELS if label in labels), "Knowledge")


def _first_value(properties: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = properties.get(key)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value[:5])
        text = str(value or "").strip()
        if text:
            return text
    return ""


def serialize_node(node_id: str, labels: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    node_type = _primary_label(list(labels or []))
    allowed = SAFE_PROPERTY_KEYS.get(node_type, set())
    safe_properties = {
        key: _safe_value(value)
        for key, value in dict(properties or {}).items()
        if key in allowed
    }
    name = _first_value(safe_properties, DISPLAY_KEYS.get(node_type, ()))
    if node_type == "Regulation":
        law_name = str(safe_properties.get("law_name") or "").strip()
        article = str(safe_properties.get("article") or "").strip()
        if law_name and article:
            name = law_name + " · " + article
    description = _first_value(
        safe_properties,
        DESCRIPTION_KEYS.get(node_type, ()),
    )
    if not name:
        name = node_type
    return {
        "id": str(node_id),
        "type": node_type,
        "name": name[:300],
        "description": description[:2_000],
        "properties": safe_properties,
    }


def serialize_curated_node(properties: dict[str, Any]) -> dict[str, Any]:
    """Serialize only the isolated BlinkKGv1 public contract."""
    raw = dict(properties or {})
    node_id = str(raw.get("id") or raw.get("kg_id") or "").strip()
    node_type = str(raw.get("type") or raw.get("entity_type") or "Knowledge").strip()
    name = str(raw.get("name") or raw.get("display_name") or node_id).strip()
    safe_properties = {
        str(key)[:80]: _safe_value(value)
        for key, value in raw.items()
        if str(key).lower() not in {
            "embedding", "vector", "kg_migration_id", "password", "token"
        }
    }
    return {
        "id": node_id,
        "type": node_type,
        "name": name[:300],
        "description": str(raw.get("description") or "")[:2_000],
        "properties": safe_properties,
    }


class KnowledgeExplorerRepository:
    """Small read-only repository; it never accepts raw Cypher from a caller."""

    def __init__(self, graph_repository: KnowledgeGraphRepository | None = None) -> None:
        self._graph = graph_repository or KnowledgeGraphRepository.from_env()

    def close(self) -> None:
        self._graph.close()

    @staticmethod
    def _curated_graph(center: dict[str, Any], edge_rows: Any) -> dict[str, Any]:
        center_node = serialize_curated_node(center)
        nodes = {center_node["id"]: center_node}
        edges: list[dict[str, Any]] = []
        for row in edge_rows:
            neighbor = serialize_curated_node(dict(row["neighbor_properties"] or {}))
            relation = str(row["relation"])
            relation_properties = dict(row["relation_properties"] or {})
            source_id = str(row["source_id"] or "")
            target_id = str(row["target_id"] or "")
            if not neighbor["id"] or not source_id or not target_id:
                continue
            nodes[neighbor["id"]] = neighbor
            label = str(
                relation_properties.get("label")
                or CURATED_RELATION_LABELS.get(relation)
                or relation
            )
            edge_id = str(
                relation_properties.get("kg_edge_id")
                or source_id + ":" + relation + ":" + target_id
            )
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
                "evidence_status": relation_properties.get("evidence_status"),
                "evidence_reference": relation_properties.get("evidence_reference"),
                "source_ids": list(relation_properties.get("source_ids") or []),
                "source_locator": relation_properties.get("source_locator"),
                "reviewed_at": relation_properties.get("reviewed_at"),
                "properties": {
                    str(key)[:80]: _safe_value(value)
                    for key, value in relation_properties.items()
                    if str(key).lower() not in {"embedding", "vector", "kg_migration_id"}
                },
            })
        return {"center": center_node, "nodes": list(nodes.values()), "edges": edges}

    def curated_catalog(self) -> list[dict[str, Any]]:
        """Bounded cloud-only entities for the dedicated map semantic cache."""
        cypher = """
        MATCH (n:BlinkKGv1)
        WHERE NOT n:EvidenceSource
        RETURN properties(n) AS properties
        ORDER BY n.id
        LIMIT 1001
        """
        with self._graph.session() as session:
            return [serialize_curated_node(dict(row["properties"] or {}))
                    for row in session.run(cypher)]

    def curated_search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """Resolve public BlinkKGv1 nodes before the legacy mixed graph."""
        normalized = " ".join(str(query or "").split()).casefold()[:80]
        if not normalized:
            return []
        safe_limit = max(1, min(int(limit), 30))
        cypher = """
        MATCH (n:BlinkKGv1)
        WHERE NOT n:EvidenceSource
        WITH n, properties(n) AS p
        WITH n, p, [p.name, p.display_name, p.normalized_name] + coalesce(p.aliases, []) AS names
        WHERE any(value IN names WHERE value IS NOT NULL
                  AND toLower(trim(toString(value))) = $needle)
        RETURN p AS properties
        ORDER BY toLower(coalesce(p.name, p.display_name, p.id, ''))
        LIMIT $limit
        """
        with self._graph.session() as session:
            return [
                serialize_curated_node(dict(row["properties"] or {}))
                for row in session.run(cypher, needle=normalized, limit=safe_limit)
            ]

    def curated_neighborhood(self, node_id: str, *, limit: int = 30) -> dict[str, Any] | None:
        """Read only reviewed relationships from the isolated public namespace."""
        stable_id = str(node_id or "").strip()[:220]
        if not stable_id:
            return None
        safe_limit = max(1, min(int(limit), 60))
        node_query = """
        MATCH (n:BlinkKGv1)
        WHERE n.id = $node_id AND NOT n:EvidenceSource
        RETURN properties(n) AS properties
        LIMIT 1
        """
        edge_query = """
        MATCH (n:BlinkKGv1)-[r]-(m:BlinkKGv1)
        WHERE n.id = $node_id
          AND NOT m:EvidenceSource
          AND type(r) IN $relations
          AND r.kg_edge_id IS NOT NULL
        RETURN properties(m) AS neighbor_properties,
               startNode(r).id AS source_id,
               endNode(r).id AS target_id,
               type(r) AS relation,
               properties(r) AS relation_properties
        ORDER BY relation, r.kg_edge_id
        LIMIT $limit
        """
        with self._graph.session() as session:
            row = session.run(node_query, node_id=stable_id).single()
            if row is None:
                return None
            center = dict(row["properties"] or {})
            edges = session.run(
                edge_query,
                node_id=stable_id,
                relations=list(CURATED_RELATIONS),
                limit=safe_limit,
            )
            return self._curated_graph(center, edges)

    def curated_explore(self, query: str, *, limit: int = 30) -> dict[str, Any] | None:
        matches = self.curated_search(query, limit=1)
        if not matches:
            return None
        graph = self.curated_neighborhood(matches[0]["id"], limit=limit)
        if graph is not None:
            graph["resolution"] = {
                "query": str(query).strip(),
                "canonical_node_id": matches[0]["id"],
                "matched_alias": None,
                "strategy": "curated-exact",
            }
        return graph

    def search(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        normalized = " ".join(str(query or "").split()).lower()[:80]
        if not normalized:
            return []
        safe_limit = max(1, min(int(limit), 30))
        cypher = """
        MATCH (n)
        WHERE any(label IN labels(n) WHERE label IN $allowed_labels)
        WITH n, labels(n) AS node_labels, properties(n) AS p
        WITH n, node_labels, p,
             coalesce(
               p.name_zh, p.name, p.name_en, p.normalized_name,
               p.risk_label, p.law_name, p.article, p.text, p.mongo_id, p.sku, ''
             ) AS primary_text,
             coalesce(p.aliases, []) AS aliases
        WHERE (p.status IS NULL OR p.status = 'active')
          AND (
            toLower(toString(primary_text)) CONTAINS $query
            OR any(alias IN aliases WHERE toLower(toString(alias)) CONTAINS $query)
          )
        RETURN elementId(n) AS id, node_labels AS labels, p AS properties,
               CASE
                 WHEN toLower(toString(primary_text)) = $query THEN 0
                 WHEN toLower(toString(primary_text)) STARTS WITH $query THEN 1
                 ELSE 2
               END AS rank
        ORDER BY rank ASC, toLower(toString(primary_text)) ASC
        LIMIT $limit
        """
        with self._graph.session() as session:
            rows = session.run(
                cypher,
                allowed_labels=list(ALLOWED_LABELS),
                query=normalized,
                limit=safe_limit,
            )
            return [
                serialize_node(row["id"], row["labels"], dict(row["properties"] or {}))
                for row in rows
            ]

    def ingredient_graph(self, query: str, *, limit: int = 18) -> dict[str, Any] | None:
        """Return a first-hop Ingredient graph from Neo4j, without seed data."""
        normalized = " ".join(str(query or "").split()).casefold()[:80]
        if not normalized:
            return None
        safe_limit = max(1, min(int(limit), 30))
        node_query = """
        MATCH (n:Ingredient)
        WHERE n.status IS NULL OR n.status = 'active'
        WITH n, properties(n) AS p,
             [n.name_zh, n.name_en, n.normalized_name] + coalesce(n.aliases, []) AS names
        WHERE any(name IN names WHERE name IS NOT NULL
                  AND toLower(toString(name)) CONTAINS $query)
        WITH n, p, names,
             CASE WHEN any(name IN names WHERE name IS NOT NULL
                           AND toLower(toString(name)) = $query)
                  THEN 0 ELSE 1 END AS rank
        RETURN elementId(n) AS id, labels(n) AS labels, p AS properties
        ORDER BY rank, toLower(coalesce(n.name_zh, n.name_en, n.normalized_name, ''))
        LIMIT 1
        """
        edge_query = """
        MATCH (n:Ingredient)-[r]-(m)
        WHERE elementId(n) = $node_id
          AND (m.status IS NULL OR m.status = 'active')
          AND ((type(r) = 'CONFLICTS_WITH' AND m:Ingredient)
               OR (type(r) = 'SUITABLE_FOR_CONCERN' AND m:SkinConcern)
               OR (type(r) = 'APPLIES_TO_RULE' AND m:RiskRule))
        RETURN elementId(m) AS neighbor_id, labels(m) AS neighbor_labels,
               properties(m) AS neighbor_properties,
               elementId(startNode(r)) AS source_id,
               elementId(endNode(r)) AS target_id,
               type(r) AS relation, properties(r) AS relation_properties
        ORDER BY relation, neighbor_id
        LIMIT $limit
        """
        with self._graph.session() as session:
            row = session.run(node_query, parameters={"query": normalized}).single()
            if row is None:
                return None
            center = serialize_node(row["id"], row["labels"], dict(row["properties"] or {}))
            center["properties"].pop("mongo_id", None)
            nodes = {center["id"]: center}
            edges = []
            for edge in session.run(edge_query, node_id=center["id"], limit=safe_limit):
                relation = str(edge["relation"])
                neighbor = serialize_node(
                    edge["neighbor_id"], edge["neighbor_labels"],
                    dict(edge["neighbor_properties"] or {}),
                )
                neighbor["properties"].pop("mongo_id", None)
                source_id = str(edge["source_id"])
                target_id = str(edge["target_id"])
                source_properties = (
                    dict(row["properties"] or {}) if source_id == center["id"]
                    else dict(edge["neighbor_properties"] or {})
                )
                evidence_status, evidence_reference = classify_edge_evidence(
                    relation, dict(edge["relation_properties"] or {}), source_properties,
                )
                relation_label = RELATION_LABELS.get(relation, relation)
                if evidence_status == "needs-review":
                    relation_label = {
                        "SUITABLE_FOR_CONCERN": "膚況資訊",
                        "APPLIES_TO_RULE": "注意事項",
                        "CONFLICTS_WITH": "成分搭配資料",
                    }.get(relation, "資料庫關係")
                nodes[neighbor["id"]] = neighbor
                edges.append({
                    "id": source_id + ":" + relation + ":" + target_id,
                    "source": source_id,
                    "target": target_id,
                    "relation": relation,
                    "relation_label": relation_label,
                    "evidence_status": evidence_status,
                    "evidence_reference": evidence_reference,
                })
        return {
            "center": center,
            "nodes": list(nodes.values()),
            "edges": edges,
            "resolution": {"query": query.strip(), "strategy": "neo4j_ingredient"},
        }

    def ingredient_mentions(self, messages: list[str], *, limit: int = 2) -> list[str]:
        """Match user questions against active Neo4j Ingredient names and aliases."""
        questions = list(dict.fromkeys(text.strip()[:160] for text in messages if text.strip()))[:40]
        if not questions:
            return []
        safe_limit = max(1, min(int(limit), 5))
        cypher = """
        UNWIND $questions AS question
        MATCH (ingredient:Ingredient)
        WHERE ingredient.status IS NULL OR ingredient.status = 'active'
        WITH question, ingredient,
             [ingredient.name_zh, ingredient.name_en] + coalesce(ingredient.aliases, []) AS names
        WHERE any(name IN names WHERE name IS NOT NULL
                  AND size(trim(toString(name))) >= 2
                  AND toLower(question) CONTAINS toLower(trim(toString(name))))
        WITH coalesce(ingredient.name_zh, ingredient.name_en) AS name, question
        WHERE name IS NOT NULL AND trim(toString(name)) <> ''
        RETURN name, count(DISTINCT question) AS mentions
        ORDER BY mentions DESC, name ASC
        LIMIT $limit
        """
        with self._graph.session() as session:
            rows = session.run(cypher, questions=questions, limit=safe_limit)
            return [str(row["name"]).strip() for row in rows]

    def semantic_search(
        self,
        embedding: list[float],
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return approved knowledge nodes ranked by cosine similarity.

        This read-only query is limited to Ingredient and SkinConcern. Product
        nodes stay in the existing product recommender and cannot leak into the
        knowledge map.
        """
        if not embedding or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in embedding
        ):
            raise ValueError("embedding must contain numeric values")
        safe_limit = max(1, min(int(limit), 30))
        cypher = """
        MATCH (n)
        WHERE any(label IN labels(n) WHERE label IN $allowed_labels)
          AND n.embedding IS NOT NULL
          AND size(n.embedding) = size($embedding)
          AND (n.status IS NULL OR n.status = 'active')
        WITH n, labels(n) AS node_labels, properties(n) AS p,
             vector.similarity.cosine(n.embedding, $embedding) AS score
        WHERE score IS NOT NULL
        RETURN elementId(n) AS id, node_labels AS labels, p AS properties,
               score
        ORDER BY score DESC
        LIMIT $limit
        """
        with self._graph.session() as session:
            rows = session.run(
                cypher,
                allowed_labels=["Ingredient", "SkinConcern"],
                embedding=[float(value) for value in embedding],
                limit=safe_limit,
            )
            results: list[dict[str, Any]] = []
            for row in rows:
                node = serialize_node(
                    row["id"], row["labels"], dict(row["properties"] or {})
                )
                node["semantic_score"] = round(float(row["score"]), 6)
                results.append(node)
            return results

    def neighborhood(self, node_id: str, *, limit: int = 18) -> dict[str, Any] | None:
        normalized_id = str(node_id or "").strip()[:220]
        if not normalized_id:
            return None
        safe_limit = max(1, min(int(limit), 30))
        node_query = """
        MATCH (n)
        WHERE elementId(n) = $node_id
          AND any(label IN labels(n) WHERE label IN $allowed_labels)
        RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties
        LIMIT 1
        """
        edge_query = """
        MATCH (n)-[r]-(m)
        WHERE elementId(n) = $node_id
          AND any(label IN labels(n) WHERE label IN $allowed_labels)
          AND any(label IN labels(m) WHERE label IN $allowed_labels)
        RETURN elementId(m) AS neighbor_id,
               labels(m) AS neighbor_labels,
               properties(m) AS neighbor_properties,
               elementId(startNode(r)) AS source_id,
               elementId(endNode(r)) AS target_id,
               type(r) AS relation,
               properties(r) AS relation_properties
        ORDER BY relation ASC
        LIMIT $limit
        """
        with self._graph.session() as session:
            row = session.run(
                node_query,
                node_id=normalized_id,
                allowed_labels=list(ALLOWED_LABELS),
            ).single()
            if row is None:
                return None
            center = serialize_node(row["id"], row["labels"], dict(row["properties"] or {}))
            nodes: dict[str, dict[str, Any]] = {center["id"]: center}
            edges: list[dict[str, Any]] = []
            for edge in session.run(
                edge_query,
                node_id=normalized_id,
                allowed_labels=list(ALLOWED_LABELS),
                limit=safe_limit,
            ):
                neighbor = serialize_node(
                    edge["neighbor_id"],
                    edge["neighbor_labels"],
                    dict(edge["neighbor_properties"] or {}),
                )
                relation = str(edge["relation"])
                source_id = str(edge["source_id"])
                target_id = str(edge["target_id"])
                relation_properties = dict(edge["relation_properties"] or {})
                source_node = center if center["id"] == source_id else neighbor
                evidence_status, evidence_reference = classify_edge_evidence(
                    relation,
                    relation_properties,
                    dict(source_node.get("properties") or {}),
                )
                if evidence_status == "needs-review" and not include_unreviewed_relations():
                    continue
                nodes[neighbor["id"]] = neighbor
                edges.append({
                    "id": source_id + ":" + relation + ":" + target_id,
                    "source": source_id,
                    "target": target_id,
                    "relation": relation,
                    "relation_label": RELATION_LABELS.get(relation, relation),
                    "evidence_status": evidence_status,
                    "evidence_reference": evidence_reference,
                    "properties": {
                        str(key)[:80]: _safe_value(value)
                        for key, value in relation_properties.items()
                        if str(key).lower() not in {"embedding", "vector"}
                    },
                })
        return {"center": center, "nodes": list(nodes.values()), "edges": edges}
