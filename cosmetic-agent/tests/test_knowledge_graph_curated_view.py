import pytest

from app.services.knowledge_graph_view import apply_graph_view


@pytest.mark.parametrize(
    "query,center_type,relation,target_type,direction",
    [
        ("酒糟肌", "SkinConcern", "HAS_TRIGGER", "Trigger", "outgoing"),
        ("泛紅", "Symptom", "REQUIRES_CAUTION", "SafetyWarning", "outgoing"),
        ("保濕", "CareNeed", "SUPPORTS_NEED", "Ingredient", "incoming"),
        ("玻尿酸", "Ingredient", "HAS_FUNCTION", "Function", "outgoing"),
        ("A酸", "AliasTerm", "MAY_REFER_TO", "DrugActiveIngredient", "outgoing"),
        ("皮爽", "AliasTerm", "MAY_REFER_TO", "DrugProduct", "outgoing"),
    ],
)
def test_six_core_terms_keep_curated_projection(query, center_type, relation, target_type, direction):
    center = {"id": f"center:{query}", "type": center_type, "name": query}
    neighbor = {"id": f"neighbor:{query}", "type": target_type, "name": "關聯節點"}
    if direction == "incoming":
        source, target = neighbor["id"], center["id"]
    else:
        source, target = center["id"], neighbor["id"]
    graph = {
        "center": center,
        "nodes": [center, neighbor],
        "edges": [{
            "id": f"edge:{query}", "source": source, "target": target,
            "relation": relation, "source_ids": ["reviewed-source"],
            "evidence_status": "verified", "evidence_reference": "reviewed claim",
        }],
    }
    result = apply_graph_view(graph, allowed_relations={relation})
    assert [edge["id"] for edge in result["edges"]] == [f"edge:{query}"]
    assert result["edges"][0]["direction_from_center"] == direction
    assert result["edges"][0]["source_ids"] == ["reviewed-source"]
    assert result["edges"][0]["evidence_reference"] == "reviewed claim"


def test_new_public_relations_and_node_types_are_presented():
    center = {"id": "concern", "type": "SkinConcern", "name": "酒糟性皮膚炎"}
    nodes = [
        center,
        {"id": "symptom", "type": "Symptom", "name": "泛紅"},
        {"id": "trigger", "type": "Trigger", "name": "日曬"},
        {"id": "warning", "type": "SafetyWarning", "name": "不作診斷"},
    ]
    edges = [
        {"id": "s", "source": "concern", "target": "symptom", "relation": "HAS_SYMPTOM"},
        {"id": "t", "source": "concern", "target": "trigger", "relation": "HAS_TRIGGER"},
        {"id": "w", "source": "concern", "target": "warning", "relation": "REQUIRES_CAUTION"},
    ]
    result = apply_graph_view({"center": center, "nodes": nodes, "edges": edges})
    assert {edge["relation"] for edge in result["edges"]} == {
        "HAS_SYMPTOM", "HAS_TRIGGER", "REQUIRES_CAUTION",
    }
    by_id = {node["id"]: node for node in result["nodes"]}
    assert by_id["trigger"]["entity_category"] == "trigger"
    assert by_id["warning"]["entity_category"] == "safety_warning"
