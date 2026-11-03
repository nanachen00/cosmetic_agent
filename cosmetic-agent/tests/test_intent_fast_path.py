from schemas.intent import IntentParseResult, IntentType, ProductCategory, SkinType
from tools.agent import intent_parser


def test_clear_product_query_does_not_call_terra(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("Terra was called")

    monkeypatch.setattr(intent_parser, "_call_openai", fail_if_called)
    result = intent_parser.parse_intent(
        "幫我找適合敏感肌、無香料、預算 1,000 元以內的保濕精華。"
    )
    assert result.intent_type == IntentType.FIND_PRODUCT
    assert result.product_category == ProductCategory.SERUM
    assert result.skin_type == SkinType.SENSITIVE
    assert result.budget_range == "1000"


def test_product_knowledge_question_still_uses_terra():
    assert intent_parser._fast_parse_obvious_product_query(
        "精華液是什麼？有什麼功效？"
    ) is None


def test_fast_path_can_be_disabled(monkeypatch):
    monkeypatch.setenv("INTENT_PRODUCT_FAST_PATH", "0")
    assert intent_parser._fast_parse_obvious_product_query(
        "幫我找適合敏感肌的保濕精華液"
    ) is None


def _result(intent_type, confidence, ambiguity):
    return IntentParseResult(
        intent_type=intent_type,
        routing_confidence=confidence,
        ambiguity_score=ambiguity,
        raw_query="測試",
    )


def test_low_fallback_runs_only_for_unresolved_none_result(monkeypatch):
    monkeypatch.setattr(intent_parser, "OPENAI_REASONING_EFFORT", "none")
    monkeypatch.setenv("INTENT_LOW_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("INTENT_LOW_FALLBACK_THRESHOLD", "0.5")
    unclear = _result(IntentType.UNCLEAR, 0.4, 0.9)
    assert intent_parser._should_try_low_fallback(unclear, True)
    assert not intent_parser._should_try_low_fallback(unclear, False)


def test_low_fallback_accepts_clear_rescue(monkeypatch):
    monkeypatch.setenv("INTENT_LOW_FALLBACK_THRESHOLD", "0.5")
    primary = _result(IntentType.UNCLEAR, 0.3, 0.9)
    candidate = _result(IntentType.KNOWLEDGE_QUERY, 0.75, 0.2)
    assert intent_parser._prefer_low_fallback(primary, candidate)


def test_low_fallback_never_flips_a_stable_non_unclear_route(monkeypatch):
    monkeypatch.setenv("INTENT_LOW_FALLBACK_THRESHOLD", "0.5")
    primary = _result(IntentType.FIND_PRODUCT, 0.65, 0.2)
    candidate = _result(IntentType.SYMPTOM_SOLVING, 0.95, 0.1)
    assert not intent_parser._prefer_low_fallback(primary, candidate)


def test_product_flow_context_repairs_elliptical_constraints():
    parsed = {"intent_type": "unclear"}
    guarded = intent_parser._apply_routing_context_guard(
        parsed,
        "清爽、不黏、三百元內",
        {"active_flow": "product_search"},
    )
    assert guarded["intent_type"] == "find_product"


def test_typo_in_product_request_uses_fast_path_without_llm(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM was called")

    monkeypatch.setattr(intent_parser, "_call_openai", fail_if_called)
    result = intent_parser.parse_intent("幫我推建敏感肌用的防曬")
    assert result.intent_type == IntentType.FIND_PRODUCT


def test_concern_sanitizer_drops_memory_noise_and_normalizes_aliases():
    assert intent_parser._sanitize_concerns(
        ["保濕", "捧油", "拗狘", "抗老", "保濕"]
    ) == ["保濕", "逆齡"]


def test_brand_only_product_request_uses_product_route_without_llm(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM was called")

    monkeypatch.setattr(intent_parser, "_call_openai", fail_if_called)
    result = intent_parser.parse_intent("請推薦給我Dr.G的產品")
    assert result.intent_type == IntentType.FIND_PRODUCT
    assert result.product_category == ProductCategory.UNKNOWN
    assert result.brand_preference == ["Dr.G"]


def test_chinese_brand_only_product_request_is_preserved(monkeypatch):
    monkeypatch.setattr(
        intent_parser,
        "_call_openai",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM was called")),
    )
    result = intent_parser.parse_intent("請推薦給我理膚寶水的產品")
    assert result.intent_type == IntentType.FIND_PRODUCT
    assert result.brand_preference == ["理膚寶水"]


def test_generic_product_request_does_not_invent_a_brand(monkeypatch):
    monkeypatch.setattr(
        intent_parser,
        "_call_openai",
        lambda *args, **kwargs: {"intent_type": "unclear"},
    )
    result = intent_parser.parse_intent("請推薦給我適合我的產品")
    assert result.brand_preference == []


def test_requested_product_words_use_local_fast_path(monkeypatch):
    monkeypatch.setattr(
        intent_parser,
        "_call_openai",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM was called")),
    )
    examples = (
        ("找去角質", ProductCategory.EXFOLIANT),
        ("找去角質凝膠", ProductCategory.EXFOLIANT),
        ("找粉刺貼", ProductCategory.PORE_STRIP),
        ("找卸妝產品", ProductCategory.MAKEUP_REMOVER),
        ("找卸妝油", ProductCategory.MAKEUP_REMOVER),
        ("找眼霜", ProductCategory.EYE_CARE),
        ("找眼周產品", ProductCategory.EYE_CARE),
        ("找護唇膏", ProductCategory.LIP_CARE),
        ("找護唇產品", ProductCategory.LIP_CARE),
        ("找痘痘凝膠", ProductCategory.SPOT_CARE),
        ("找局部抗痘產品", ProductCategory.SPOT_CARE),
        ("找抗痘痘產品", ProductCategory.SPOT_CARE),
        ("找抗痘產品", ProductCategory.SPOT_CARE),
        ("找抗痘面膜", ProductCategory.MASK),
        ("找抗痘精華", ProductCategory.SERUM),
    )
    for text, category in examples:
        result = intent_parser.parse_intent(text)
        assert result.intent_type == IntentType.FIND_PRODUCT, text
        assert result.product_category == category, text


def test_exfoliant_knowledge_question_not_forced_to_products():
    assert intent_parser._fast_parse_obvious_product_query("去角質怎麼用") is None
