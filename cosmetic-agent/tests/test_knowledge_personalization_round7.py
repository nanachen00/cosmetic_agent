from app.services.knowledge_explorer_service import KnowledgeExplorerService


def sample_graph():
    return {
        "center": {"id": "salicylic", "name": "水楊酸", "type": "ingredient"},
        "nodes": [
            {"id": "salicylic", "name": "水楊酸", "type": "ingredient"},
            {"id": "acne", "name": "粉刺與毛孔阻塞", "type": "concern"},
            {"id": "fragrance", "name": "香精刺激風險", "type": "safety"},
            {"id": "hydration", "name": "日常保濕", "type": "care"},
        ],
        "edges": [],
    }


def test_personalization_prioritizes_recent_context_and_cautions():
    result = KnowledgeExplorerService.personalize_graph(sample_graph(), {
        "skin_type": "油性",
        "concerns": ["粉刺"],
        "allergens": ["香精"],
        "recent_issues": ["痘痘"],
        "worsening_issues": ["泛紅"],
        "recent_record_count": 12,
    })

    assert result["personalization"]["active"] is True
    assert "fragrance" in result["personalization"]["caution_node_ids"]
    assert "acne" in result["personalization"]["priority_node_ids"]
    assert result["nodes"][0]["id"] == "fragrance"
    assert result["nodes"][0]["personalization"]["level"] == "caution"
    assert result["center"]["id"] == "salicylic"


def test_personalization_is_neutral_without_authenticated_context():
    result = KnowledgeExplorerService.personalize_graph(sample_graph(), None)

    assert result["personalization"]["active"] is False
    assert [node["id"] for node in result["nodes"]] == [
        "salicylic", "acne", "fragrance", "hydration",
    ]
    assert all("personalization" not in node for node in result["nodes"])


def test_viewer_context_keeps_only_bounded_supported_fields():
    result = KnowledgeExplorerService._viewer_context({
        "skin_type": "敏感性\n膚質",
        "recent_issues": ["泛紅", "泛紅", "刺痛"],
        "worsening_issues": ["泛紅"],
        "recent_products": ["保濕乳"],
        "recent_record_count": 999,
        "private_note": "must not pass through",
    })

    assert result["skin_type"] == "敏感性 膚質"
    assert result["recent_issues"] == ["泛紅", "刺痛"]
    assert result["recent_record_count"] == 30
    assert "private_note" not in result
