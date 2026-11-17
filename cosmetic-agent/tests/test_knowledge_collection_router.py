"""Explicit symptom routing must reach retrieval without inventing symptoms."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.knowledge_retrieval_service import KnowledgeRetrievalService
from tools.search.knowledge_intent import KnowledgeIntentParser


@pytest.mark.parametrize("symptom", ["脫皮", "乾燥", "緊繃", "刺痛", "紅腫", "過敏"])
def test_explicit_symptoms_select_skin_concerns(symptom):
    route = KnowledgeIntentParser._route_by_rules("臉頰" + symptom + "怎麼辦？")
    assert "skin_concerns" in route
    assert "ingredients" not in route


def test_reported_dryness_case_reaches_skin_tool_and_keeps_risk_last():
    query = "臉頰乾燥脫皮，洗臉後緊繃，日常應怎麼保養？"
    route = KnowledgeIntentParser._route_by_rules(query)
    assert route == ["risk_rules", "skin_concerns"]
    assert KnowledgeRetrievalService.order_collections(route) == ["skin_concerns", "risk_rules"]
    skin = Mock()
    skin.search_concern.return_value = {"name": "fixture concern"}
    extractor, lookup, risk = Mock(), Mock(), Mock()
    service = KnowledgeRetrievalService(
        graph_repository=SimpleNamespace(session=lambda: nullcontext(object())),
        ingredient_extractor=extractor, ingredient_lookup=lookup, skin_tool=skin,
        risk_tool=risk, observe_stage=lambda *a, **kw: None,
    )
    result = service.retrieve(query=query, ordered_collections=route,
                              intent_result=None, user_skin_type="unknown")
    skin.search_concern.assert_called_once_with(query)
    assert result["skin_concern_info"] == {"name": "fixture concern"}
    extractor.extract_ingredient.assert_not_called()
    lookup.get_node_data.assert_not_called()


@pytest.mark.parametrize("query", ["可以用嗎", "能不能用", "有什麼禁忌"])
def test_safety_question_without_symptoms_does_not_invent_skin_retrieval(query):
    assert KnowledgeIntentParser._route_by_rules(query) == ["risk_rules"]


@pytest.mark.parametrize("query", ["水楊酸是什麼", "玻尿酸有什麼功用", "retinol有什麼作用"])
def test_pure_ingredient_questions_preserve_ingredient_only_route(query):
    assert KnowledgeIntentParser._route_by_rules(query) == ["ingredients"]


def test_mixed_symptom_ingredient_safety_preserves_dependency_order():
    route = KnowledgeIntentParser._route_by_rules("臉頰刺痛，可以用水楊酸嗎？")
    assert KnowledgeRetrievalService.order_collections(route) == ["skin_concerns", "ingredients", "risk_rules"]
