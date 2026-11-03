from types import SimpleNamespace

from agents.root.orchestrator import RootAgentOrchestrator, _explicit_skin_consult_intent
from schemas.intent import (
    IntentParseResult,
    IntentType,
    ProductCategory,
    calculate_routing_confidence,
    calculate_task_completeness,
)
from tools.agent.intent_parser import _apply_obvious_product_route_guard


def _result(
    intent_type: IntentType,
    *,
    concern: list[str] | None = None,
    product_category: ProductCategory = ProductCategory.UNKNOWN,
) -> IntentParseResult:
    return IntentParseResult(
        intent_type=intent_type,
        concern=concern or [],
        product_category=product_category,
        raw_query="測試問題",
    )


def test_explicit_skin_consult_reuses_known_intent_without_model_parse():
    parsed = _explicit_skin_consult_intent("臉頰乾燥泛紅怎麼辦？")

    assert parsed.intent_type == IntentType.SYMPTOM_SOLVING
    assert parsed.raw_query == "臉頰乾燥泛紅怎麼辦？"
    assert parsed.routing_confidence == 1.0
    assert parsed.task_completeness == 1.0
    assert parsed.ambiguity_score == 0.0


def test_routing_confidence_is_separate_from_task_completeness():
    vague_product = _result(IntentType.FIND_PRODUCT)

    assert calculate_routing_confidence(vague_product) == 1.0
    assert calculate_task_completeness(vague_product) == 0.4


def test_product_agent_can_start_with_a_concern_or_category():
    with_concern = _result(IntentType.FIND_PRODUCT, concern=["乾燥"])
    with_category = _result(
        IntentType.RECOMMENDATION,
        product_category=ProductCategory.SERUM,
    )

    assert calculate_task_completeness(with_concern) == 1.0
    assert calculate_task_completeness(with_category) == 1.0


def test_knowledge_and_comparison_queries_do_not_require_product_profile():
    knowledge = _result(IntentType.KNOWLEDGE_QUERY)
    comparison = _result(IntentType.PRICE_COMPARISON)

    assert calculate_task_completeness(knowledge) == 1.0
    assert calculate_task_completeness(comparison) == 1.0


def test_unclear_query_has_no_routing_confidence():
    unclear = _result(IntentType.UNCLEAR)

    assert calculate_routing_confidence(unclear) == 0.0
    assert calculate_task_completeness(unclear) == 0.0


def test_obvious_product_search_is_not_treated_as_unclear():
    parsed = {"intent_type": "unclear"}
    guarded = _apply_obvious_product_route_guard(parsed, "我想找保養品")

    assert guarded["intent_type"] == "find_product"


def test_unrelated_find_request_stays_unclear():
    guarded = _apply_obvious_product_route_guard(
        {"intent_type": "unclear"}, "幫我找資料"
    )


def test_unclear_route_asks_function_before_skin_type():
    turns = []
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    root.state = SimpleNamespace(
        is_waiting_clarification=False,
        clarify_dimension="",
        intent=_result(IntentType.UNCLEAR),
        add_turn=lambda role, content: turns.append((role, content)),
    )

    result = root._ask_clarification("routing")

    assert result["stage"] == "clarification_routing"
    assert "膚質" not in result["reply"]
    assert len(result["quick_replies"]) == 5
    assert any("使用心得" in item["label"] for item in result["quick_replies"])


def test_product_concern_answer_dispatches_without_fixed_followups():
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    root.state = SimpleNamespace(
        is_waiting_clarification=True,
        clarify_dimension="concern",
        skin_type_skipped=False,
        intent=_result(IntentType.FIND_PRODUCT),
    )
    root._do_dispatch = lambda intent: {
        "stage": "dispatch",
        "intent": intent.model_dump(),
    }

    result = root._handle_clarification_reply("乾燥脫皮")

    assert result["stage"] == "dispatch"
    assert result["intent"]["concern"] == ["乾燥脫皮"]
    assert result["intent"]["task_completeness"] == 1.0


def _clarification_root():
    return RootAgentOrchestrator.__new__(RootAgentOrchestrator)


def test_natural_concern_sentence_is_kept_as_the_answer():
    root = _clarification_root()
    answer = "最近乾燥脫皮，希望不要太黏"

    assert root._normalize_clarification_answer("concern", answer) == answer


def test_explicit_topic_switch_is_not_consumed_as_a_concern_answer():
    root = _clarification_root()

    assert root._normalize_clarification_answer("concern", "另外想問 A 酸是什麼？") is None


def test_natural_skin_type_sentence_is_understood():
    root = _clarification_root()

    assert root._normalize_clarification_answer("skin_type", "我是乾性肌，而且最近會脫皮") == "dry"


def test_confirm_yes_dispatches_the_pending_product_intent():
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    pending_intent = _result(
        IntentType.FIND_PRODUCT,
        product_category=ProductCategory.CREAM,
    )
    root.state = SimpleNamespace(
        is_waiting_confirm=True,
        intent=pending_intent,
    )
    root._do_dispatch = lambda intent: {
        "stage": "dispatch",
        "intent": intent,
    }

    result = root._handle_confirm_reply("confirm_yes")

    assert root.state.is_waiting_confirm is False
    assert result["stage"] == "dispatch"
    assert result["intent"] is pending_intent
