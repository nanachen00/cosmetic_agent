import pytest

from tools.agent.product_turn_resolver import resolve_product_turn


PRODUCT_STATE = {
    "active_flow": "product_search",
    "current_step": "product_results",
    "intent_type": "find_product",
    "product_category": "精華液",
    "has_products": True,
}


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("推薦一款保濕精華", "精華液"),
        ("幫我選防曬", "防曬乳"),
        ("防曬哪款適合", "防曬乳"),
        ("面膜", "面膜"),
    ],
)
def test_clear_product_entry_bypasses_intent_parser(text, category):
    result = resolve_product_turn(text, conversation_flow="auto")
    assert result["route"] == "product_search"
    assert result["requires_intent_parser"] is False
    assert result["intent_patch"]["product_category"] == category


@pytest.mark.parametrize("text", ["請推薦一個", "有推薦嗎", "推薦東西"])
def test_vague_product_entry_asks_category_without_llm(text):
    result = resolve_product_turn(text, conversation_flow="auto")
    assert result["route"] == "product_search"
    assert result["needs_clarification"] is True
    assert result["requires_intent_parser"] is False


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("再多給我幾個", "more_products"),
        ("看更多商品", "more_products"),
        ("換一批，不要重複剛剛的", "replace_batch"),
        ("我想換一瓶", "replace_batch"),
    ],
)
def test_product_pagination_actions_are_distinct(text, action):
    result = resolve_product_turn(
        text, conversation_flow="product_search", structured_state=PRODUCT_STATE
    )
    assert result["action"] == action
    assert result["requires_intent_parser"] is False


def test_category_change_is_not_misread_as_replace_batch():
    result = resolve_product_turn(
        "換個面膜", conversation_flow="product_search", structured_state=PRODUCT_STATE
    )
    assert result["action"] is None
    assert result["route"] == "product_search"
    assert result["inherit_context"] is True
    assert result["intent_patch"]["product_category"] == "面膜"


def test_compound_constraint_refinement_is_local_and_structured():
    result = resolve_product_turn(
        "清爽、不要黏，三百塊左右",
        conversation_flow="product_search",
        structured_state=PRODUCT_STATE,
    )
    assert result["requires_intent_parser"] is False
    assert result["inherit_context"] is True
    assert result["intent_patch"]["texture_preference"] == "清爽"
    assert result["intent_patch"]["budget_range"] == "300"


def test_skin_type_refinement_inherits_product_context():
    result = resolve_product_turn(
        "有沒有適合敏感肌的",
        conversation_flow="product_search",
        structured_state=PRODUCT_STATE,
    )
    assert result["route"] == "product_search"
    assert result["inherit_context"] is True
    assert result["intent_patch"]["skin_type"] == "sensitive"


def test_product_knowledge_question_does_not_trigger_cards():
    result = resolve_product_turn("防曬好用嗎", conversation_flow="auto")
    assert result["route"] == "skin_consult"
    assert result["requires_intent_parser"] is False
    contextual = resolve_product_turn(
        "防曬好用嗎", conversation_flow="product_search", structured_state=PRODUCT_STATE
    )
    assert contextual["route"] == "skin_consult"


def test_skin_record_flow_is_never_stolen_by_product_rules():
    result = resolve_product_turn(
        "敷面膜後臉頰泛紅", conversation_flow="skin_record"
    )
    assert result["route"] == "unresolved"
    assert result["requires_intent_parser"] is True


@pytest.mark.parametrize("text", ["這個呢", "我不確定", "剛才那個怎麼辦"])
def test_genuinely_ambiguous_turns_still_use_semantic_parser(text):
    result = resolve_product_turn(text, conversation_flow="auto")
    assert result["route"] == "unresolved"
    assert result["requires_intent_parser"] is True


def test_explicit_action_wins_without_text_guessing():
    result = resolve_product_turn(
        "按鈕顯示文字不重要",
        conversation_flow="product_search",
        structured_state=PRODUCT_STATE,
        action="cycle_more",
    )
    assert result["action"] == "more_products"
    assert result["reason"] == "explicit_structured_action"
    assert result["requires_intent_parser"] is False


@pytest.mark.parametrize("text", ["找痘痘貼", "找痘貼", "找 acne patch"])
@pytest.mark.parametrize(
    "conversation_flow,structured_state",
    [("auto", None), ("product_search", PRODUCT_STATE)],
)
def test_acne_patch_category_in_new_and_existing_product_turns(
    text, conversation_flow, structured_state
):
    result = resolve_product_turn(
        text,
        conversation_flow=conversation_flow,
        structured_state=structured_state,
    )
    assert result["route"] == "product_search"
    assert result["requires_intent_parser"] is False
    assert result["intent_patch"]["product_category"] == "acne_patch"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("找去角質", "exfoliant"),
        ("找去角質凝膠", "exfoliant"),
        ("找粉刺貼", "pore_strip"),
        ("找卸妝產品", "makeup_remover"),
        ("找卸妝油", "makeup_remover"),
        ("找眼霜", "eye_care"),
        ("找眼周產品", "eye_care"),
        ("找護唇膏", "lip_care"),
        ("找護唇產品", "lip_care"),
        ("找痘痘凝膠", "spot_care"),
        ("找局部抗痘產品", "spot_care"),
        ("找抗痘痘產品", "spot_care"),
        ("找抗痘產品", "spot_care"),
        ("找抗痘面膜", "面膜"),
        ("找抗痘精華", "精華液"),
    ],
)
@pytest.mark.parametrize(
    ("conversation_flow", "structured_state"),
    [("auto", None), ("product_search", PRODUCT_STATE)],
)
def test_requested_categories_in_new_and_existing_product_turns(
    text, category, conversation_flow, structured_state
):
    result = resolve_product_turn(
        text, conversation_flow=conversation_flow, structured_state=structured_state
    )
    assert result["route"] == "product_search"
    assert result["requires_intent_parser"] is False
    assert result["intent_patch"]["product_category"] == category


def test_exfoliant_usage_question_keeps_knowledge_route():
    result = resolve_product_turn("去角質怎麼用", conversation_flow="auto")
    assert result["route"] == "skin_consult"
