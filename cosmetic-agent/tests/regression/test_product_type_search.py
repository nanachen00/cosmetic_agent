import pytest

from agents.product.product_agent import (
    _PRODUCT_TYPE_BY_CATEGORY,
    _matches_requested_category,
)
from schemas.intent import IntentType, ProductCategory
from tools.agent import intent_parser


def test_all_known_product_names_have_type_mapping():
    aliases = [
        (alias, category)
        for category, names in intent_parser._PRODUCT_CATEGORY_ALIASES
        for alias in names
    ]
    assert len({name.casefold() for name, _ in aliases}) >= 101
    for alias, category in aliases:
        assert intent_parser._match_product_category(alias) == category
        assert category.value in _PRODUCT_TYPE_BY_CATEGORY


@pytest.mark.parametrize(
    ("query", "category"),
    [
        ("找痘痘貼", ProductCategory.ACNE_PATCH),
        ("找粉刺貼", ProductCategory.PORE_STRIP),
        ("找眼膜", ProductCategory.EYE_CARE),
        ("找唇膜", ProductCategory.LIP_CARE),
        ("找卸妝乳", ProductCategory.MAKEUP_REMOVER),
        ("找精華化妝水", ProductCategory.TONER),
        ("找防曬精華", ProductCategory.SUNSCREEN),
        ("找淡斑精華", ProductCategory.SERUM),
        ("找去角質泥", ProductCategory.EXFOLIANT),
        ("找潔顏慕斯", ProductCategory.CLEANSER),
        ("痘痘貼", ProductCategory.ACNE_PATCH),
    ],
)
def test_specific_names_win_over_generic_words(query, category):
    result = intent_parser.parse_intent(query)
    assert result.intent_type == IntentType.FIND_PRODUCT
    assert result.product_category == category


@pytest.mark.parametrize(
    "query", ["找痘印修護產品", "找粉刺調理產品", "找毛孔調理產品"]
)
def test_concern_only_product_request_does_not_invent_type(query):
    result = intent_parser.parse_intent(query)
    assert result.intent_type == IntentType.FIND_PRODUCT
    assert result.product_category == ProductCategory.UNKNOWN


def test_stored_type_is_authoritative_over_broad_category():
    eye_serum = {
        "name": "眼部精華", "category": "精華液", "product_type": "eye_care"
    }
    assert _matches_requested_category(eye_serum, "eye_care")
    assert not _matches_requested_category(eye_serum, "精華液")
    assert not _matches_requested_category(eye_serum, "lip_care")


def test_untyped_product_keeps_legacy_name_fallback():
    item = {"name": "溫和粉刺貼", "category": "臉部保養"}
    assert _matches_requested_category(item, "pore_strip")
    assert not _matches_requested_category(item, "acne_patch")


def test_face_lotion_rejects_body_and_makeup_products():
    body_lotion = {
        "name": "淡斑勻亮精華凝乳",
        "category": "身體乳液",
        "product_type": "emulsion",
        "description": "身體肌膚保濕用",
    }
    tone_up_lotion = {
        "name": "提亮保濕乳",
        "category": "乳液",
        "description": "三色膠囊可呈現妝效與覆蓋效果",
    }
    face_lotion = {
        "name": "清爽保濕乳",
        "category": "乳液",
        "description": "臉部日常保濕",
    }

    assert not _matches_requested_category(body_lotion, "乳液")
    assert not _matches_requested_category(tone_up_lotion, "乳液")
    assert _matches_requested_category(face_lotion, "乳液")


def test_category_search_passes_product_type_to_neo4j(monkeypatch):
    from agents.product import product_agent as agent

    captured = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def run(self, query, **parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return []

    class FakeDriver:
        def session(self):
            return FakeSession()

    monkeypatch.setattr(agent, "get_query_embedding", lambda _: [0.1, 0.2])
    monkeypatch.setattr(agent, "get_driver", lambda: FakeDriver())
    assert agent.product_search("粉刺貼", category="pore_strip", top_k=5) == []
    assert captured["parameters"]["product_type"] == "pore_strip"
    assert "node.product_type = $product_type" in captured["query"]
