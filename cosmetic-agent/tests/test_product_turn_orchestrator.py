from types import SimpleNamespace

from agents.root.orchestrator import RootAgentOrchestrator
from schemas.intent import IntentParseResult, IntentType, ProductCategory


def _orchestrator_with(previous=None):
    agent = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    agent.state = SimpleNamespace(intent=previous)
    agent._log = lambda *args, **kwargs: None
    agent._refresh_intent_scores = lambda: None
    agent._do_dispatch = lambda intent: {"intent": intent.model_dump()}
    agent._ask_clarification = lambda dimension: {"clarify": dimension}
    agent._handle_structured_action = (
        lambda action, text: {"action": action, "text": text}
    )
    return agent


def test_resolved_refinement_inherits_category_and_overrides_constraints():
    previous = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        product_category=ProductCategory.SERUM,
        budget_range="500",
        raw_query="推薦保濕精華",
    )
    agent = _orchestrator_with(previous)
    result = agent._handle_resolved_product_turn(
        "清爽、三百塊左右",
        {
            "route": "product_search",
            "action": None,
            "reason": "product_context_refinement",
            "inherit_context": True,
            "needs_clarification": False,
            "intent_patch": {
                "budget_range": "300",
                "texture_preference": "清爽",
            },
        },
    )
    intent = result["intent"]
    assert intent["intent_type"] == "find_product"
    assert intent["product_category"] == "精華液"
    assert intent["budget_range"] == "300"
    assert intent["texture_preference"] == "清爽"


def test_resolved_category_change_does_not_keep_old_category():
    previous = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        product_category=ProductCategory.SERUM,
        raw_query="推薦精華",
    )
    agent = _orchestrator_with(previous)
    result = agent._handle_resolved_product_turn(
        "換個面膜",
        {
            "route": "product_search",
            "action": None,
            "reason": "product_context_refinement",
            "inherit_context": True,
            "needs_clarification": False,
            "intent_patch": {"product_category": "面膜"},
        },
    )
    assert result["intent"]["product_category"] == "面膜"


def test_resolved_knowledge_question_dispatches_without_product_intent():
    agent = _orchestrator_with()
    result = agent._handle_resolved_product_turn(
        "防曬好用嗎",
        {
            "route": "skin_consult",
            "action": None,
            "reason": "product_knowledge_question",
            "inherit_context": False,
            "needs_clarification": False,
            "intent_patch": {},
        },
    )
    assert result["intent"]["intent_type"] == "knowledge_query"


def test_resolved_vague_request_asks_for_category():
    agent = _orchestrator_with()
    result = agent._handle_resolved_product_turn(
        "請推薦一個",
        {
            "route": "product_search",
            "action": None,
            "reason": "vague_product_request",
            "inherit_context": False,
            "needs_clarification": True,
            "intent_patch": {},
        },
    )
    assert result == {"clarify": "product_category"}


def test_resolved_action_bypasses_intent_creation():
    agent = _orchestrator_with()
    result = agent._handle_resolved_product_turn(
        "再多給我幾個",
        {
            "route": "product_search",
            "action": "more_products",
            "reason": "more_products_phrase",
            "inherit_context": True,
            "needs_clarification": False,
            "intent_patch": {},
        },
    )
    assert result["action"] == "more_products"
