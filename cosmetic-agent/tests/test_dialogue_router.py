import pytest

from tools.agent.dialogue_router import (
    classify_dialogue_route,
    conversation_recall_reply,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("我擦 A 酸後脫皮刺痛，怎麼辦？", "skin_consult"),
        ("最近皮膚怪怪的", "clarify"),
        ("幫我記錄今天臉很乾燥", "skin_record"),
        ("幫我記錄今天泛紅，順便推薦乳霜", "multi_intent"),
        ("嘴唇腫而且喘不過氣", "emergency"),
        ("我沒有呼吸困難，只有輕微乾燥", "skin_consult"),
        ("嗨", "small_talk"),
        ("你好！", "small_talk"),
        ("謝謝～", "small_talk"),
        ("hi", "small_talk"),
        ("我想……", "incomplete"),
        ("請問", "incomplete"),
        ("我的皮膚", "incomplete"),
        ("可以幫我", "incomplete"),
        ("最近皮膚很乾", "skin_consult"),
        ("嗨，我想找無香精防曬", None),
        ("預算 1000、無香精的防曬", None),
        ("cycle_skin_tracking", None),
        ("__knowledge_consult__:A 酸有什麼副作用", None),
        ("幫我寫 Python", None),
    ],
)
def test_classify_dialogue_route(message, expected):
    decision = classify_dialogue_route(message)
    assert (decision and decision["route"]) == expected


@pytest.mark.parametrize(
    "message",
    [
        "沒有呼吸困難，也沒有嘴唇腫，只是有點乾",
        "並無喘不過氣，只有輕微泛紅",
    ],
)
def test_negated_emergency_is_not_escalated(message):
    decision = classify_dialogue_route(message)
    assert not decision or decision["route"] != "emergency"


def test_conversation_recall_uses_only_matching_user_statement():
    context = "\n".join(
        [
            "使用者先前訊息：今天左臉泛紅，測試代號是藍盒。",
            "使用者先前訊息：我先前說的是左臉還是右臉？",
        ]
    )
    reply = conversation_recall_reply(
        "我先前在本對話說的是哪一邊臉泛紅？",
        context,
    )
    assert reply is not None
    assert "左臉泛紅" in reply


def test_conversation_recall_does_not_invent_from_prior_questions():
    context = "\n".join(
        [
            "使用者先前訊息：測試代號是什麼？",
            "使用者先前訊息：我先前在本對話說的是左臉還是右臉？",
        ]
    )
    reply = conversation_recall_reply(
        "我先前在本對話說的是哪一邊臉泛紅？",
        context,
    )
    assert reply == "本對話沒有提過。"


def test_normal_skin_question_is_not_intercepted_as_recall():
    assert conversation_recall_reply(
        "我之前用了 A 醇，現在泛紅怎麼辦？",
        "使用者先前訊息：今天左臉泛紅。",
    ) is None


@pytest.mark.parametrize(
    "message",
    [
        "眼皮突然腫起來而且呼吸不順",
        "眼皮突然腫、呼吸不太順，還能繼續擦嗎？",
        "嘴唇腫脹、一直喘，喉嚨也很緊",
        "眼周腫到睜不開，感覺吸不到氣",
    ],
)
def test_airway_plus_eye_or_lip_swelling_is_intercepted_before_llm(message):
    decision = classify_dialogue_route(message)
    assert decision and decision["route"] == "emergency"


@pytest.mark.parametrize(
    ("message", "flow", "expected_action"),
    [
        ("看更多商品", "product_search", "more_products"),
        ("換一批，不要跟剛剛重複", "product_search", "replace_batch"),
        ("不是右臉，是左臉", "skin_record", "record_correction"),
        ("嚴重度改成 4/5", "skin_record", "record_severity"),
        ("確認儲存", "skin_record", "record_confirm"),
        ("取消", "skin_record", "cancel"),
    ],
)
def test_structured_actions_are_not_forced_into_general_intents(
    message, flow, expected_action
):
    decision = classify_dialogue_route(
        message,
        conversation_flow=flow,
        structured_state={"skin_session_active": flow == "skin_record"},
    )
    assert decision and decision.get("action") == expected_action


def test_explicit_structured_action_is_authoritative():
    decision = classify_dialogue_route(
        "顯示文字不應決定實際操作",
        conversation_flow="product_search",
        action="more_products",
    )
    assert decision and decision.get("action") == "more_products"


def test_product_requirement_confirmation_is_not_rewritten_as_skin_record():
    decision = classify_dialogue_route(
        "confirm_yes",
        conversation_flow="product_search",
        current_step="intent_confirmation",
        structured_state={"is_waiting_confirm": True},
    )

    assert decision is None


def test_requirement_confirmation_wins_over_stale_skin_flow_context():
    decision = classify_dialogue_route(
        "confirm_yes",
        conversation_flow="skin_record",
        current_step="intent_confirmation",
        structured_state={
            "active_flow": "skin_record",
            "is_waiting_confirm": True,
            "skin_session_active": True,
        },
    )

    assert not decision or decision.get("action") != "record_confirm"


def test_skin_record_confirmation_still_maps_to_record_confirm():
    decision = classify_dialogue_route(
        "confirm_yes",
        conversation_flow="skin_record",
        current_step="skin_tracking:confirm",
        structured_state={"skin_session_active": True},
    )

    assert decision and decision.get("action") == "record_confirm"


@pytest.mark.parametrize(
    ("message", "flow", "expected_route"),
    [
        ("今天兩頰有點乾", "skin_record", "skin_record"),
        ("毛孔", "skin_consult", "clarify"),
        ("清爽、不黏、三百元內", "product_search", "product_search"),
        ("我該先擦精華還是乳霜？", "product_search", "skin_consult"),
    ],
)
def test_conversation_flow_participates_in_routing(message, flow, expected_route):
    decision = classify_dialogue_route(message, conversation_flow=flow)
    assert decision and decision["route"] == expected_route


def test_history_resolves_context_dependent_followup():
    decision = classify_dialogue_route(
        "這樣正常嗎？",
        conversation_flow="skin_consult",
        history=[{"role": "user", "content": "擦 A 酸後一直脫皮刺痛"}],
    )
    assert decision and decision["route"] == "skin_consult"


def test_explicit_record_location_action_is_routed_without_intent_guessing():
    decision = classify_dialogue_route(
        "left_cheek",
        conversation_flow="skin_record",
        action="record_location",
    )

    assert decision == {"route": "action", "reason": "structured_dialogue_action", "action": "record_location"}
