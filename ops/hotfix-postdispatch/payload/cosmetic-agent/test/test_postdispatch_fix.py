"""Regression tests for the 2026-10-29 post-dispatch hotfix (review L1–L3).

L4/O1 (response composer) are covered in test_response_composer.py.
"""

import pytest

import agents.root.orchestrator as orchestrator
import tools.a2a.skin_product_bridge as skin_bridge
from agents.root.orchestrator import ConversationState, RootAgentOrchestrator, _is_confirmation
from schemas.intent import IntentParseResult, IntentType
from schemas.tracking_schema import ISSUE_LABELS, Issue

# product_agent ranks concerns by these CONCERN_SYNONYM keys (agents/product/product_agent.py).
PRODUCT_CONCERNS = {"保濕", "抗痘", "控油", "美白", "舒敏", "修護", "抗老"}
# 痘疤 could mean 美白 or 修護; it stays unmapped until the team decides. other has no concern.
UNMAPPED_ISSUES = {Issue.acne_scar, Issue.other, Issue.dark_spot}


# ── L1 skin bridge ────────────────────────────────────────────────


def _skin_context(monkeypatch, issues, severity=4):
    records = [{"issue": issue, "severity": severity} for issue in issues]
    monkeypatch.setattr(skin_bridge.postgres_skin_record_store, "query", lambda user_id: records)
    return skin_bridge.get_skin_context("u1")


def test_every_mapped_concern_is_a_product_concern():
    assert set(skin_bridge._CONDITION_TO_CONCERN.values()) <= PRODUCT_CONCERNS


def test_tracking_issue_values_and_labels_map_to_product_concerns(monkeypatch):
    issues = [issue for issue in Issue if issue not in UNMAPPED_ISSUES]
    by_value = _skin_context(monkeypatch, [issue.value for issue in issues])["concern_hints"]
    by_label = _skin_context(monkeypatch, [ISSUE_LABELS[issue] for issue in issues])["concern_hints"]
    assert by_value == by_label == ["抗痘", "保濕", "舒敏", "控油", "美白"]
    assert _skin_context(monkeypatch, ["dark_spot"])["concern_hints"] == ["美白"]


def test_chinese_high_severity_is_flagged(monkeypatch):
    assert _skin_context(monkeypatch, ["dryness"], severity="嚴重")["risk_flags"] == ["dryness_high_severity"]


# ── L2 confirmation reply ─────────────────────────────────────────


@pytest.mark.parametrize("reply", [
    "confirm_yes", "next", "對", "是", "好", "好的", "ok", "OK", "Yes", "確認", "沒錯", "正確",
    "對，就是這樣", "✅ 對，就是這樣", "好的謝謝！", "嗯嗯", "下一步",
])
def test_affirmative_replies_confirm(reply):
    assert _is_confirmation(reply) is True


@pytest.mark.parametrize("reply", [
    "confirm_no", "需要調整", "❌ 需要調整", "不是", "不對", "不好", "不用，我要乳液", "沒有，我是乾肌",
    "是，但我是油肌", "我是油肌", "對，可是預算要 500 以內", "no", "", "   ",
])
def test_negations_and_corrections_do_not_confirm(reply):
    assert _is_confirmation(reply) is False


@pytest.mark.parametrize("reply", ["不是", "不對", "不好", "不是啦", "錯了", "No", "需要調整", "❌ 需要調整", "confirm_no"])
def test_bare_rejections_are_recognised(reply):
    assert orchestrator._is_bare_rejection(reply) is True


@pytest.mark.parametrize("reply", ["沒有，我是乾肌", "不是，我是油肌", "不要乳液", "對", "", "confirm_yes"])
def test_rejections_with_content_are_not_bare(reply):
    assert orchestrator._is_bare_rejection(reply) is False


def _confirm_root(monkeypatch):
    parsed = []
    monkeypatch.setattr(orchestrator, "parse_intent", lambda **kwargs: parsed.append(kwargs["user_query"]))
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    root.model = "stub"
    root._intent_memory_context = lambda: ""
    root._routing_context = lambda: {}
    root.state = ConversationState()
    root.state.pain_gain = {}
    root.state.intent = IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query="乾燥保濕精華")
    root.state.is_waiting_confirm = True
    root._do_dispatch = lambda intent: {"stage": "dispatch", "intent": intent}
    return root, parsed


@pytest.mark.parametrize("reply", ["不是", "不對", "confirm_no"])
def test_bare_rejection_keeps_the_intent_and_asks_what_to_change(monkeypatch, reply):
    root, parsed = _confirm_root(monkeypatch)
    pending = root.state.intent
    result = root._handle_confirm_reply(reply)
    assert result["stage"] == "pain_gain_confirm_adjust"
    assert parsed == []
    assert root.state.intent is pending
    assert root.state.is_waiting_confirm is True
    assert root._handle_confirm_reply("confirm_yes")["intent"] is pending


def test_rejection_with_content_is_parsed_again(monkeypatch):
    root, parsed = _confirm_root(monkeypatch)
    result = root._handle_confirm_reply("沒有，我是乾肌")
    assert result["stage"] == "pain_gain_confirm_retry"
    assert parsed == ["沒有，我是乾肌"]
    assert root.state.is_waiting_confirm is True


def test_affirmative_confirm_reply_dispatches(monkeypatch):
    root, _ = _confirm_root(monkeypatch)
    assert root._handle_confirm_reply("對，就是這樣")["stage"] == "dispatch"


# ── L3 emergency before profile query ─────────────────────────────


class _ProfileBridge:
    def answer_profile_question(self, message, profile):
        return "您的膚質是乾性肌。" if "膚質" in message else None


def _turn_root():
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    root.state = ConversationState()
    root.profile_bridge = _ProfileBridge()
    root.basic_profile = {}
    root._profile_load_error = False
    return root


def test_emergency_is_answered_before_a_profile_question():
    result = _turn_root()._process_current_turn("我是什麼膚質？我剛擦完精華嘴唇腫起來而且呼吸困難")
    assert result["stage"] == "emergency"
    assert "119" in result["reply"]


def test_plain_profile_question_still_answers_from_profile():
    result = _turn_root()._process_current_turn("我是什麼膚質？")
    assert result["stage"] == "profile_query"
