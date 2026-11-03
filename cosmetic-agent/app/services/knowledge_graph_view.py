"""Public view profiles for the knowledge-map API.

The source graph may contain many reviewed relations.  The map UI should not
render all of them at once.  This module classifies the centre entity, selects
the relations useful for that entity type, and emits a stable edge contract.
It never creates a node or a relation that is absent from the source graph.
"""

from __future__ import annotations

from typing import Any, AbstractSet


ENTITY_CATEGORIES: dict[str, tuple[str, str]] = {
    "SkinConcern": ("skin_concern", "膚況"),
    "Symptom": ("symptom", "症狀"),
    "Trigger": ("trigger", "誘因"),
    "SafetyWarning": ("safety_warning", "安全提醒"),
    "CareNeed": ("care_need", "保養需求"),
    "Function": ("skincare_function", "保養功能"),
    "Ingredient": ("ingredient", "保養成分"),
    "IngredientCategory": ("ingredient_category", "成分類別"),
    "DrugActiveIngredient": ("drug_active_ingredient", "藥品有效成分"),
    "DrugProduct": ("drug_product", "藥品／商品名"),
    "AliasTerm": ("alias", "別名／俗名"),
    "ConcernCategory": ("concern_category", "膚況分類"),
    "RiskRule": ("risk_notice", "注意事項"),
    "ApprovedIndication": ("approved_indication", "核准適應症"),
    "Manufacturer": ("manufacturer", "製造商"),
    "DrugClass": ("drug_class", "藥品分類"),
    "Category": ("category", "分類"),
    "Product": ("product", "產品"),
}


RELATION_PRESENTATIONS: dict[str, dict[str, str]] = {
    "ACTIVE_IN": {"label": "有效成分", "inverse": "含此有效成分", "group": "藥品資料"},
    "ALIAS_OF": {"label": "又稱", "inverse": "常見叫法", "group": "名稱"},
    "APPLIES_TO_RULE": {"label": "使用注意", "inverse": "適用對象", "group": "注意事項"},
    "BELONGS_TO_CATEGORY": {"label": "屬於", "inverse": "包含", "group": "分類"},
    "BELONGS_TO_DRUG_CLASS": {"label": "藥品分類", "inverse": "包含藥品", "group": "藥品資料"},
    "CONFLICTS_WITH": {"label": "不建議同時使用", "inverse": "不建議同時使用", "group": "搭配注意"},
    "CONTAINS": {"label": "含有成分", "inverse": "可見於產品", "group": "成分"},
    "HAS_APPROVED_INDICATION": {"label": "核准適應症", "inverse": "核准用於", "group": "藥品資料"},
    "HAS_FUNCTION": {"label": "主要作用", "inverse": "具有此作用的成分", "group": "作用"},
    "HAS_SYMPTOM": {"label": "常見症狀", "inverse": "可能相關狀況", "group": "症狀"},
    "HAS_TRIGGER": {"label": "常見誘因", "inverse": "可能誘發", "group": "誘因"},
    "MANUFACTURED_BY": {"label": "製造商", "inverse": "生產藥品", "group": "藥品資料"},
    "MAY_INCREASE_IRRITATION_WITH": {"label": "可能增加刺激", "inverse": "可能增加刺激", "group": "搭配注意"},
    "MAY_REFER_TO": {"label": "可能指的是", "inverse": "可能叫做", "group": "名稱"},
    "MENTIONS_CONCERN": {"label": "涉及膚況", "inverse": "相關資料", "group": "膚況"},
    "NAVIGATES_TO_CONCERN": {"label": "查看相關膚況", "inverse": "可由此名詞找到", "group": "膚況"},
    "NAVIGATES_TO_INGREDIENT": {"label": "查看相關成分", "inverse": "可由此需求找到", "group": "成分"},
    "REGULATES_CATEGORY": {"label": "規範品類", "inverse": "受此規範", "group": "法規"},
    "RELATED_CONCERN": {"label": "相關膚況", "inverse": "相關成分", "group": "膚況"},
    "RELEVANT_TO_CARE": {"label": "照護方向", "inverse": "適用情境", "group": "保養方向"},
    "REQUIRES_CAUTION": {"label": "使用／判讀注意", "inverse": "適用於此提醒", "group": "注意事項"},
    "REQUIRES_FUNCTION": {"label": "需要的功能", "inverse": "對應保養需求", "group": "保養方向"},
    "SUPPORTS_NEED": {"label": "可支援需求", "inverse": "代表成分", "group": "保養方向"},
    "SUITABLE_FOR_CONCERN": {"label": "可能適用", "inverse": "可考慮成分", "group": "膚況"},
    "USE_WITH_CAUTION_FOR": {"label": "特定膚況慎用", "inverse": "需留意成分", "group": "注意事項"},
    "WARNS_CONCERN": {"label": "可能影響", "inverse": "需留意風險", "group": "注意事項"},
}


VIEW_PROFILES: dict[str, tuple[str, ...]] = {
    "SkinConcern": (
        "HAS_SYMPTOM", "HAS_TRIGGER", "REQUIRES_CAUTION",
        "RELEVANT_TO_CARE", "RELATED_CONCERN", "BELONGS_TO_CATEGORY",
        "SUITABLE_FOR_CONCERN", "WARNS_CONCERN", "MENTIONS_CONCERN",
        "USE_WITH_CAUTION_FOR", "MAY_INCREASE_IRRITATION_WITH",
    ),
    "Symptom": (
        "HAS_SYMPTOM", "REQUIRES_CAUTION", "NAVIGATES_TO_CONCERN",
        "MENTIONS_CONCERN", "RELEVANT_TO_CARE",
    ),
    "Trigger": ("HAS_TRIGGER",),
    "SafetyWarning": ("REQUIRES_CAUTION",),
    "CareNeed": (
        "REQUIRES_FUNCTION", "SUPPORTS_NEED", "REQUIRES_CAUTION",
        "NAVIGATES_TO_INGREDIENT", "RELEVANT_TO_CARE",
    ),
    "Function": ("HAS_FUNCTION", "REQUIRES_FUNCTION", "NAVIGATES_TO_INGREDIENT"),
    "Ingredient": (
        "HAS_FUNCTION", "SUPPORTS_NEED", "RELEVANT_TO_CARE", "RELATED_CONCERN",
        "SUITABLE_FOR_CONCERN", "APPLIES_TO_RULE", "CONFLICTS_WITH",
        "MAY_INCREASE_IRRITATION_WITH", "USE_WITH_CAUTION_FOR",
        "NAVIGATES_TO_CONCERN",
    ),
    "IngredientCategory": ("BELONGS_TO_CATEGORY", "HAS_FUNCTION", "SUPPORTS_NEED"),
    "DrugActiveIngredient": (
        "ACTIVE_IN", "HAS_FUNCTION", "HAS_APPROVED_INDICATION", "BELONGS_TO_DRUG_CLASS",
        "MAY_REFER_TO", "MENTIONS_CONCERN", "USE_WITH_CAUTION_FOR", "REQUIRES_CAUTION",
    ),
    "DrugProduct": (
        "ACTIVE_IN", "HAS_APPROVED_INDICATION", "MANUFACTURED_BY",
        "BELONGS_TO_DRUG_CLASS",
    ),
    "AliasTerm": ("ALIAS_OF", "MAY_REFER_TO"),
    "ConcernCategory": ("BELONGS_TO_CATEGORY", "NAVIGATES_TO_CONCERN"),
    "RiskRule": (
        "APPLIES_TO_RULE", "WARNS_CONCERN", "USE_WITH_CAUTION_FOR",
        "MAY_INCREASE_IRRITATION_WITH",
    ),
    "ApprovedIndication": ("HAS_APPROVED_INDICATION",),
    "Manufacturer": ("MANUFACTURED_BY",),
    "DrugClass": ("BELONGS_TO_DRUG_CLASS",),
    "Category": ("BELONGS_TO_CATEGORY", "CONTAINS", "REGULATES_CATEGORY"),
    "Product": ("CONTAINS",),
}


_EVIDENCE_ORDER = {
    "reviewed": 0,
    "documented": 1,
    "source-linked": 2,
    "verified": 3,
    "derived": 4,
    "needs-review": 5,
    "ontology-review": 6,
    "derived-review": 7,
    "unknown": 8,
}


def _decorate_node(node: dict[str, Any]) -> dict[str, Any]:
    result = dict(node)
    category, label = ENTITY_CATEGORIES.get(
        str(result.get("type") or "Knowledge"),
        ("unknown", "其他"),
    )
    result["entity_category"] = category
    result["type_label_zh"] = label
    return result


def _source_ids(edge: dict[str, Any]) -> list[str]:
    values = edge.get("source_ids") or []
    if isinstance(values, str):
        values = [values]
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _evidence_reference(edge: dict[str, Any]) -> str | None:
    direct = str(edge.get("evidence_reference") or "").strip()
    if direct:
        return direct[:500]
    properties = dict(edge.get("properties") or {})
    for key in ("evidence_reference", "reference", "source_url", "source", "url"):
        value = str(properties.get(key) or "").strip()
        if value:
            return value[:500]
    return None


def _direction_from_center(edge: dict[str, Any], center_id: str) -> str:
    if str(edge.get("source") or "") == center_id:
        return "outgoing"
    if str(edge.get("target") or "") == center_id:
        return "incoming"
    return "connected"


def _dedupe_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    """Collapse duplicate/mirrored copies without changing canonical direction."""
    endpoints = sorted((str(edge.get("source") or ""), str(edge.get("target") or "")))
    return (str(edge.get("relation") or ""), endpoints[0], endpoints[1])


def apply_graph_view(
    graph: dict[str, Any],
    *,
    allowed_relations: AbstractSet[str] | None = None,
    expandable_edge_limit: int = 24,
) -> dict[str, Any]:
    """Return a typed, deduplicated UI projection of an existing graph."""
    result = dict(graph)
    center = _decorate_node(dict(graph["center"]))
    center_id = str(center["id"])
    center_type = str(center.get("type") or "Knowledge")
    profile_relations = VIEW_PROFILES.get(center_type, ())
    if allowed_relations is not None:
        profile_relations = tuple(
            relation for relation in profile_relations if relation in allowed_relations
        )
    relation_priorities = {
        relation: index for index, relation in enumerate(profile_relations)
    }

    source_nodes = {
        str(node.get("id") or ""): _decorate_node(dict(node))
        for node in graph.get("nodes") or []
        if str(node.get("id") or "")
    }
    source_nodes[center_id] = center
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw_edge in graph.get("edges") or []:
        edge = dict(raw_edge)
        relation = str(edge.get("relation") or "")
        presentation = RELATION_PRESENTATIONS.get(relation)
        if presentation is None or relation not in relation_priorities:
            continue
        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        if not source_id or not target_id or source_id not in source_nodes or target_id not in source_nodes:
            continue
        status = str(edge.get("evidence_status") or "unknown")
        direction_from_center = _direction_from_center(edge, center_id)
        edge.update({
            "id": str(edge.get("id") or f"{source_id}:{relation}:{target_id}"),
            "source": source_id,
            "target": target_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "label_zh": presentation["label"],
            "inverse_label_zh": presentation["inverse"],
            "relation_label": presentation["label"],
            "direction": "source_to_target",
            "direction_from_center": direction_from_center,
            "display_label_zh": (
                presentation["inverse"]
                if direction_from_center == "incoming"
                else presentation["label"]
            ),
            "display_group": presentation["group"],
            "display_priority": relation_priorities[relation],
            "evidence_status": status,
            "source_ids": _source_ids(edge),
            "evidence_reference": _evidence_reference(edge),
        })
        key = _dedupe_key(edge)
        current = selected.get(key)
        score = (
            relation_priorities[relation],
            _EVIDENCE_ORDER.get(status, 99),
            0 if direction_from_center == "outgoing" else 1,
            edge["id"],
        )
        if current is None or score < current[0]:
            selected[key] = (score, edge)

    edges = [item[1] for item in sorted(selected.values(), key=lambda item: item[0])]
    edges = edges[: max(1, min(int(expandable_edge_limit), 60))]
    node_ids = {center_id}
    for edge in edges:
        node_ids.add(edge["source"])
        node_ids.add(edge["target"])
    nodes = [source_nodes[node_id] for node_id in source_nodes if node_id in node_ids]

    result["center"] = center
    result["nodes"] = nodes
    result["edges"] = edges
    result["view"] = {
        "profile": center["entity_category"],
        "center_type": center_type,
        "center_type_label_zh": center["type_label_zh"],
        "visible_relations": list(profile_relations),
        "desktop_initial_nodes": 8,
        "mobile_initial_nodes": 5,
        "expandable": len(edges) > 5,
    }
    return result
