from app.services.knowledge_graph_view import apply_graph_view


def _node(node_id, node_type, name=None):
    return {"id": node_id, "type": node_type, "name": name or node_id}


def _edge(edge_id, source, target, relation, **extra):
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "relation": relation,
        **extra,
    }


def test_skin_concern_uses_a_typed_public_view():
    graph = {
        "center": _node("rosacea", "SkinConcern", "酒糟肌"),
        "nodes": [
            _node("rosacea", "SkinConcern", "酒糟肌"),
            _node("care", "CareNeed", "舒緩"),
            _node("maker", "Manufacturer", "不應顯示的製造商"),
        ],
        "edges": [
            _edge(
                "care-edge", "rosacea", "care", "RELEVANT_TO_CARE",
                evidence_status="reviewed", source_ids=["care_guideline_v1"],
            ),
            _edge("maker-edge", "rosacea", "maker", "MANUFACTURED_BY"),
        ],
    }

    result = apply_graph_view(graph)

    assert result["center"]["entity_category"] == "skin_concern"
    assert result["view"]["profile"] == "skin_concern"
    assert [edge["id"] for edge in result["edges"]] == ["care-edge"]
    assert result["edges"][0]["label_zh"] == "照護方向"
    assert result["edges"][0]["direction"] == "source_to_target"
    assert result["edges"][0]["direction_from_center"] == "outgoing"
    assert result["edges"][0]["source_ids"] == ["care_guideline_v1"]


def test_reverse_traversal_has_an_inverse_display_label():
    graph = {
        "center": _node("hydration", "CareNeed", "保濕"),
        "nodes": [
            _node("hydration", "CareNeed", "保濕"),
            _node("function", "Function", "吸濕"),
        ],
        "edges": [
            _edge(
                "needs", "hydration", "function", "REQUIRES_FUNCTION",
                evidence_status="derived",
            ),
        ],
    }

    result = apply_graph_view(graph)

    assert result["view"]["profile"] == "care_need"
    assert result["edges"][0]["display_label_zh"] == "需要的功能"
    assert result["view"]["mobile_initial_nodes"] == 5


def test_mirrored_duplicate_edges_collapse_to_one_canonical_edge():
    graph = {
        "center": _node("retinoic", "DrugActiveIngredient", "A 酸"),
        "nodes": [
            _node("retinoic", "DrugActiveIngredient", "A 酸"),
            _node("drug", "DrugProduct", "痘痘藥"),
        ],
        "edges": [
            _edge(
                "reviewed", "retinoic", "drug", "ACTIVE_IN",
                evidence_status="reviewed",
            ),
            _edge(
                "mirrored", "drug", "retinoic", "ACTIVE_IN",
                evidence_status="unknown",
            ),
        ],
    }

    result = apply_graph_view(graph)

    assert [edge["id"] for edge in result["edges"]] == ["reviewed"]
    assert result["edges"][0]["label_zh"] == "有效成分"


def test_related_to_and_unknown_relations_are_never_fabricated():
    graph = {
        "center": _node("ingredient", "Ingredient", "玻尿酸"),
        "nodes": [
            _node("ingredient", "Ingredient", "玻尿酸"),
            _node("other", "Knowledge", "其他"),
        ],
        "edges": [
            _edge("vague", "ingredient", "other", "RELATED_TO"),
            _edge("invented", "ingredient", "other", "LLM_INVENTED"),
        ],
    }

    result = apply_graph_view(graph)

    assert result["edges"] == []
    assert [node["id"] for node in result["nodes"]] == ["ingredient"]



def test_every_public_edge_has_complete_relation_contract():
    graph = {
        "center": _node("ingredient", "Ingredient", "菸鹼醯胺"),
        "nodes": [
            _node("ingredient", "Ingredient", "菸칼醯胺"),
            _node("function", "Function", "提亮"),
        ],
        "edges": [
            _edge(
                "function-edge", "ingredient", "function", "HAS_FUNCTION",
                properties={"source_url": "https://example.test/evidence"},
            )
        ],
    }

    edge = apply_graph_view(graph)["edges"][0]

    assert edge["label_zh"] == "主要作用"
    assert edge["inverse_label_zh"] == "具有此作用的成分"
    assert edge["direction"] == "source_to_target"
    assert edge["evidence_status"] == "unknown"
    assert edge["source_ids"] == []
    assert edge["evidence_reference"] == "https://example.test/evidence"
