from app.services.knowledge_explorer_service import KnowledgeExplorerService


def test_verified_relation_removes_unjustified_review_notice():
    graph = {
        "center": {"id": "n1", "name": "維他命 C"},
        "nodes": [
            {"id": "n1", "name": "維他命 C"},
            {"id": "c1", "name": "暗沉"},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n1",
                "target": "c1",
                "relation": "SUITABLE_FOR_CONCERN",
                "relation_label": "照護關聯",
                "evidence_status": "verified",
            }
        ],
    }

    result = KnowledgeExplorerService._ensure_required_notice(
        "維他命 C 與暗沉照護有關。此資料尚待人工複核。",
        graph,
    )

    assert result == "維他命 C 與暗沉照護有關。"
