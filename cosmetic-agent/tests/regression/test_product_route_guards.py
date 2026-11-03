from tools.a2a.product_knowledge_gate import product_knowledge_gate
from tools.agent.intent_parser import _apply_knowledge_query_guard


def _product_intent(**updates):
    value = {
        "intent_type": "find_product",
        "skin_type": "unknown",
        "concern": [],
        "product_category": "不明",
        "texture_preference": "不明",
        "budget_range": None,
        "brand_preference": [],
        "urgency_flag": False,
        "ambiguity_score": 0.5,
        "query_expansion": [],
        "raw_query": "",
    }
    value.update(updates)
    return value


def test_specific_product_detail_bypasses_recommendation_catalog():
    parsed = _apply_knowledge_query_guard(
        _product_intent(),
        "Ducato真我指甲油 36珍珠金評價如何",
    )
    assert parsed["intent_type"] == "knowledge_query"


def test_explicit_recommendation_stays_in_product_catalog():
    parsed = _apply_knowledge_query_guard(
        _product_intent(
            concern=["保濕"],
            product_category="精華液",
        ),
        "推薦保濕精華液",
    )
    assert parsed["intent_type"] == "find_product"


def test_general_recommendation_skips_model_gate():
    result = product_knowledge_gate.needs_knowledge_agent("推薦保濕精華液")
    assert result == {
        "decision": False,
        "reason": "一般商品搜尋，不需要額外知識庫判斷",
        "source": "rule",
    }


def test_explicit_safety_question_uses_knowledge_layer():
    result = product_knowledge_gate.needs_knowledge_agent(
        "這罐敏感肌用會不會刺激"
    )
    assert result["decision"] is True
    assert result["source"] == "rule"
