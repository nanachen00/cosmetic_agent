import json
from types import SimpleNamespace

from agents.root.orchestrator import RootAgentOrchestrator
from schemas.intent import IntentParseResult, IntentType
from tools.agent.response_composer import ResponseComposer


class FakeResponses:
    def __init__(self, reply: str = "", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=json.dumps({"reply": self.reply}, ensure_ascii=False))


class FakeClient:
    def __init__(self, reply: str = "", error: Exception | None = None):
        self.responses = FakeResponses(reply=reply, error=error)


def _composer(reply: str = "", error: Exception | None = None, skip_intents=()):
    # Guard tests opt every intent in; the default skip list is tested separately.
    client = FakeClient(reply=reply, error=error)
    return ResponseComposer(client=client, enabled=True, skip_intents=skip_intents), client


def test_disabled_composer_returns_the_agent_reply():
    composer = ResponseComposer(enabled=False)
    result = composer.compose(
        user_query="A酸有什麼作用？",
        intent_type="knowledge_query",
        raw_reply="A酸能加速角質代謝。",
    )

    assert result.reply == "A酸能加速角質代謝。"
    assert result.used_model is False
    assert result.fallback_reason == "disabled"


def test_knowledge_reply_uses_conversation_context():
    candidate = "A酸主要是加速角質代謝；你剛提到容易泛紅，使用時也要留意刺激。"
    composer, client = _composer(candidate)
    result = composer.compose(
        user_query="那 A 酸呢？",
        intent_type="knowledge_query",
        raw_reply="A酸主要是加速角質代謝，使用時要留意刺激。",
        conversation_context=[
            {"role": "user", "content": "我的皮膚容易泛紅"},
        ],
    )

    assert result.reply == candidate
    assert result.used_model is True
    payload = json.loads(client.responses.calls[0]["input"])
    assert payload["intent_type"] == "knowledge_query"
    assert payload["conversation_context"][0]["content"] == "我的皮膚容易泛紅"


def test_product_names_prices_and_order_can_be_preserved():
    raw = """1. Alpha 保濕精華，價格 NT$299，適合乾燥。
2. Beta 修護乳霜，價格 NT$399，適合脫皮。"""
    candidate = """以你在意的乾燥脫皮來看，可以先從這兩款選：
1. Alpha 保濕精華：價格 NT$299，偏向補水。
2. Beta 修護乳霜：價格 NT$399，偏向修護。"""
    composer, _ = _composer(candidate)
    result = composer.compose(
        user_query="乾燥脫皮適合哪一款？",
        intent_type="find_product",
        raw_reply=raw,
        protected_terms=["Alpha 保濕精華", "Beta 修護乳霜"],
    )

    assert result.reply == candidate
    assert result.used_model is True


def test_missing_price_falls_back_to_the_agent_reply():
    raw = "Alpha 保濕精華的價格是 NT$299，適合乾燥膚況。"
    composer, _ = _composer("如果你偏乾，可以考慮 Alpha 保濕精華。")
    result = composer.compose(
        user_query="這款多少錢？",
        intent_type="price_comparison",
        raw_reply=raw,
        protected_terms=["Alpha 保濕精華"],
    )

    assert result.reply == raw
    assert result.used_model is False
    assert result.fallback_reason.startswith("missing_protected_fact:")


def test_product_reordering_falls_back_to_the_agent_reply():
    raw = """1. Alpha 精華
2. Beta 乳霜"""
    composer, _ = _composer("""1. Beta 乳霜
2. Alpha 精華""")
    result = composer.compose(
        user_query="哪款更適合？",
        intent_type="recommendation",
        raw_reply=raw,
        protected_terms=["Alpha 精華", "Beta 乳霜"],
    )

    assert result.reply == raw
    assert result.fallback_reason == "protected_order_changed"


def test_safety_warning_cannot_be_removed():
    raw = "若出現持續刺痛，請立即停用並就醫。"
    composer, _ = _composer("若覺得不舒服，可以先減少使用頻率。")
    result = composer.compose(
        user_query="用了會刺痛怎麼辦？",
        intent_type="symptom_solving",
        raw_reply=raw,
    )

    assert result.reply == raw
    assert result.used_model is False
    assert "missing_protected_fact" in result.fallback_reason


def test_model_error_falls_back_without_breaking_chat():
    raw = "這是原始 Agent 回覆。"
    composer, _ = _composer(error=TimeoutError("timeout"))
    result = composer.compose(
        user_query="請推薦產品",
        intent_type="find_product",
        raw_reply=raw,
    )

    assert result.reply == raw
    assert result.used_model is False
    assert result.fallback_reason == "model_error:TimeoutError"


def test_root_passes_product_facts_and_history_to_composer():
    raw = """1. Alpha 精華，價格 NT$299。
2. Beta 乳霜，價格 NT$399。"""
    candidate = """依照你剛補充的乾燥需求：
1. Alpha 精華，價格 NT$299。
2. Beta 乳霜，價格 NT$399。"""
    composer, client = _composer(candidate)

    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root.response_composer = composer
    root._log = lambda *args: None
    root.state = SimpleNamespace(
        last_products=[
            {"name": "Alpha 精華", "brand": "A 品牌"},
            {"name": "Beta 乳霜", "brand": "B 品牌"},
        ],
        history=[{"role": "user", "content": "我是乾性肌"}],
    )
    intent = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        concern=["乾燥"],
        raw_query="幫我找保濕產品",
    )

    result = root._compose_agent_reply(intent, raw)
    payload = json.loads(client.responses.calls[0]["input"])

    assert result == candidate
    assert payload["protected_terms"] == [
        "Alpha 精華",
        "A 品牌",
        "Beta 乳霜",
        "B 品牌",
    ]
    assert payload["conversation_context"][0]["content"] == "我是乾性肌"


# ── hotfix 2026-10-29 (post-dispatch review L4/O1) ─────────────────


def test_structured_agent_replies_skip_the_rewrite_by_default(monkeypatch):
    monkeypatch.delenv("RESPONSE_COMPOSER_SKIP_INTENTS", raising=False)
    client = FakeClient(reply="改寫版")
    composer = ResponseComposer(client=client, enabled=True)
    for intent_type in (
        "knowledge_query",
        "symptom_solving",
        "find_product",
        "recommendation",
        "price_comparison",
    ):
        result = composer.compose(user_query="A酸呢？", intent_type=intent_type, raw_reply="A酸能加速角質代謝。")
        assert result.reply == "A酸能加速角質代謝。"
        assert result.fallback_reason == f"skipped_intent:{intent_type}"
    assert client.responses.calls == []


def test_skip_list_can_be_cleared_from_the_environment(monkeypatch):
    monkeypatch.setenv("RESPONSE_COMPOSER_SKIP_INTENTS", "")
    client = FakeClient(reply="A酸主要是加速角質代謝。")
    composer = ResponseComposer(client=client, enabled=True)
    result = composer.compose(user_query="A酸呢？", intent_type="knowledge_query", raw_reply="A酸能加速角質代謝。")
    assert result.used_model is True


def test_system_notice_is_never_sent_to_the_model():
    invented = "菸鹼醯胺是一種常見的藥物，主要用於控制血糖水平。"
    for raw, intent_type in (
        ("知識庫暫時維護中，請稍後再試。", "knowledge_query"),
        ("目前找到商品資料，但暫時無法整理回答，請稍後再試。", "knowledge_query"),
        ("抱歉，查詢知識庫時發生錯誤。", "find_product"),
        ("目前找不到精華的比較結果，請試試調整條件。", "price_comparison"),
    ):
        composer, client = _composer(invented)
        result = composer.compose(user_query="菸鹼醯胺有什麼功效？", intent_type=intent_type, raw_reply=raw)
        assert result.reply == raw
        assert result.fallback_reason == "system_notice"
        assert client.responses.calls == []


def test_added_number_falls_back_to_the_agent_reply():
    raw = "Alpha 保濕精華，價格 NT$299，適合乾燥。"
    composer, _ = _composer("Alpha 保濕精華含 5% 菸鹼醯胺，價格 NT$299，適合乾燥。")
    result = composer.compose(
        user_query="乾燥適合哪一款？", intent_type="find_product", raw_reply=raw, protected_terms=["Alpha 保濕精華"],
    )
    assert result.reply == raw
    assert result.fallback_reason == "added_number:5"


def test_large_expansion_falls_back_to_the_agent_reply():
    raw = "Alpha 精華適合乾燥肌。"
    composer, _ = _composer("Alpha 精華適合乾燥肌。" + "它還能改善膚況並帶來多種好處。" * 8)
    result = composer.compose(user_query="乾燥適合哪一款？", intent_type="find_product", raw_reply=raw, protected_terms=["Alpha 精華"])
    assert result.reply == raw
    assert result.fallback_reason == "expanded_beyond_raw"
