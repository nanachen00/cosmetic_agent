"""Exercise the real product run/scoring/copy pipeline with synthetic catalogues.

Only external retrieval, graph access and provider boundaries are replaced. These
tests establish deterministic application behaviour, not clinical suitability or
live catalogue accuracy.
"""
from copy import deepcopy
import math

import pytest

from agents.product import product_agent
from schemas.intent import IntentParseResult, IntentType, ProductCategory


EVIDENCE_FIELDS = {
    "personalization_policy_version",
    "personalization_reasons",
    "personalization_missing",
    "personalization_components",
    "personalization_weights",
}


def intent(**changes):
    values = {
        "intent_type": IntentType.FIND_PRODUCT,
        "product_category": ProductCategory.LOTION,
        "concern": ["保濕"],
        "raw_query": "推薦日常保濕乳液",
    }
    return IntentParseResult(**{**values, **changes})


def catalogue():
    common = {
        "category": "乳液", "price": 500, "currency": "TWD", "score": .7,
        "is_partner": True, "ingredients_list": ["Glycerin", "Squalane"],
        "ingredients_inci": ["Aqua", "Glycerin", "Squalane"],
        "ingredient_evidence_level": "full_inci", "inci_status": "full",
        "recommendation_scope": "full_formula", "formula_market": "TW",
        "inci_source_url": "https://synthetic.example/formula",
        "ingredient_source_url": "https://synthetic.example/formula",
        "price_observed_at": "2026-11-17",
        "price_source_url": "https://synthetic.example/price",
        "skin_types": ["dry", "oily"],
        "skin_types_source_url": "https://synthetic.example/skin-types",
        "skin_types_verification_status": "verified",
        "concerns": ["保濕"], "functional_categories": ["hydration_repair"],
        "recommendation_limitations": "僅限合成測試資料，不作實際適配或安全保證。",
    }
    return [
        {**deepcopy(common), "id": "light", "name": "晨露清爽凝乳", "brand": "BrandA",
         "texture": "清爽凝乳", "texture_source_url": "https://synthetic.example/light",
         "texture_verification_status": "verified",
         "source_urls": ["https://synthetic.example/light"]},
        {**deepcopy(common), "id": "rich", "name": "晚露滋潤乳液", "brand": "BrandB",
         "texture": "滋潤乳液", "texture_source_url": "https://synthetic.example/rich",
         "texture_verification_status": "verified",
         "source_urls": ["https://synthetic.example/rich"]},
    ]


@pytest.fixture
def run_offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The personalization pipeline test attempted external I/O or a provider call")

    monkeypatch.setenv("PRODUCT_COPY_USE_LLM", "0")
    monkeypatch.setenv("PRODUCT_INGREDIENT_EXTRACTOR", "deterministic")
    monkeypatch.setattr(product_agent, "get_driver", forbidden)
    monkeypatch.setattr(product_agent._terra_client, "complete", forbidden)
    monkeypatch.setattr(product_agent._embedding_client, "embed_query", forbidden)
    monkeypatch.setattr(product_agent, "generate_copy", forbidden)
    monkeypatch.setattr("tools.search.product_web_search.search_named_product", forbidden)
    monkeypatch.setattr(product_agent, "_enrich_partner_products", lambda *args: {
        "ingredients": [], "warnings": [], "product_suits": [],
    })
    searches = []

    def run(rows, preferences, request=None, **kwargs):
        def search(query, **search_options):
            searches.append((query, deepcopy(search_options)))
            return deepcopy(rows)

        monkeypatch.setattr(product_agent, "product_search", search)
        monkeypatch.setattr(product_agent, "product_search_by_brands", forbidden)
        result = product_agent.run(request or intent(), preference_context=deepcopy(preferences), **kwargs)
        for product in result["products"]:
            assert EVIDENCE_FIELDS <= product.keys()
            assert product["personalization_policy_version"] == "personal-v3"
            assert isinstance(product["personalization_reasons"], list)
            assert isinstance(product["personalization_missing"], list)
            assert isinstance(product["personalization_components"], dict)
            assert sum(product["personalization_weights"].values()) == pytest.approx(1)
            assert math.isfinite(product["personalization_score"])
            # The real deterministic copy stage must use the scorer's reasons.
            assert product["recommendation_reason"] == product["rank_reason"]
            assert any(reason in product["rank_reason"] for reason in product["personalization_reasons"][:2])
        return result

    run.searches = searches
    return run


def ids(result):
    return [row["id"] for row in result["products"]]


def test_same_query_catalogue_reverse_for_saved_texture_and_preserve_explanations(run_offline):
    rows = catalogue()
    original = deepcopy(rows)
    light = run_offline(rows, {"preferred_textures": ["清爽"], "budget_max": 1000})
    rich = run_offline(rows, {"preferred_textures": ["滋潤"], "budget_max": 1000})
    assert ids(light) == ["light", "rich"]
    assert ids(rich) == ["rich", "light"]
    assert run_offline.searches[0] == run_offline.searches[1], "Only profile changes, not query/retrieval"
    assert "偏好的清爽質地" in light["products"][0]["rank_reason"]
    assert "偏好的滋潤質地" in rich["products"][0]["rank_reason"]
    for result in (light, rich):
        assert result["personalization_applied"] is True
        for product in result["products"]:
            assert "預算" in product["rank_reason"], "Even a non-texture match states a concrete supplied criterion"
            assert product["price_observed_at"] == "2026-11-17"
            assert product["price_source_url"] == "https://synthetic.example/price"
            assert product["recommendation_limitations"] == rows[0]["recommendation_limitations"]
    assert rows == original, "A second profile cannot inherit mutations from the first request"


def test_same_query_catalogue_reverse_for_explicit_saved_brand(run_offline):
    rows = catalogue()
    a = run_offline(rows, {"preferred_brands": ["BrandA"], "budget_max": 1000})
    b = run_offline(rows, {"preferred_brands": ["BrandB"], "budget_max": 1000})
    assert ids(a) == ["light", "rich"]
    assert ids(b) == ["rich", "light"]
    assert "偏好的品牌：BrandA" in a["products"][0]["rank_reason"]
    assert "偏好的品牌：BrandB" in b["products"][0]["rank_reason"]
    assert run_offline.searches[0] == run_offline.searches[1]


def test_budget_change_reverses_first_result_by_eligibility_not_fake_price_fit(run_offline):
    rows = catalogue()
    rows[0]["price"], rows[1]["price"] = 250, 700
    narrow = run_offline(rows, {"preferred_brands": ["BrandB"], "budget_max": 300})
    broad = run_offline(rows, {"preferred_brands": ["BrandB"], "budget_max": 1000})
    assert ids(narrow) == ["light"]
    assert ids(broad) == ["rich", "light"]
    assert "NT$300" in narrow["products"][0]["rank_reason"]
    assert "NT$1000" in broad["products"][0]["rank_reason"]
    assert all(not product["over_budget"] for result in (narrow, broad) for product in result["products"])
    assert run_offline.searches[0] == run_offline.searches[1]


def test_current_texture_overrides_saved_texture_in_real_run(run_offline):
    result = run_offline(catalogue(), {"preferred_textures": ["滋潤"], "budget_max": 1000},
                         request=intent(texture_preference="清爽"))
    assert ids(result) == ["light", "rich"]
    assert "偏好的清爽質地" in result["products"][0]["rank_reason"]
    assert "偏好的滋潤質地" not in result["products"][0]["rank_reason"]


@pytest.mark.parametrize("constraint", ["allergen", "avoided_brand", "budget", "already_seen", "unknown_formula"])
def test_extreme_preference_and_marketing_scores_cannot_buy_back_hard_exclusions(run_offline, constraint):
    rows = catalogue()
    allowed, blocked = rows
    blocked.update(score=1e15, popularity_score=1e15, review_count=1000000, rating=5,
                   rating_source_url="https://synthetic.example/reviews")
    preferences = {"preferred_brands": ["BrandB"], "preferred_textures": ["滋潤"], "budget_max": 1000}
    arguments = {}
    if constraint == "allergen":
        blocked["ingredients_inci"].append("Parfum")
        arguments["disliked_ingredients"] = ["香料"]
    elif constraint == "avoided_brand":
        preferences["avoided_brands"] = ["BrandB"]
    elif constraint == "budget":
        blocked["price"] = 1001
    elif constraint == "already_seen":
        arguments["excluded_product_ids"] = ["rich"]
    elif constraint == "unknown_formula":
        blocked["inci_status"] = "missing"
        blocked["ingredient_evidence_level"] = "official_highlights"
        arguments["disliked_ingredients"] = ["香料"]
    result = run_offline(rows, preferences, **arguments)
    assert ids(result) == [allowed["id"]]
    assert result["total_candidates"] == 1


def test_missing_evidence_remains_explicit_after_copy_and_no_fame_is_invented(run_offline):
    rows = [catalogue()[0]]
    rows[0].pop("price_observed_at")
    result = run_offline(rows, {"preferred_textures": ["清爽"], "budget_max": 1000})
    product = result["products"][0]
    assert "價格更新時間待確認" in product["personalization_missing"]
    assert any("評論樣本數或來源" in text for text in product["personalization_missing"])
    assert "price_observed_at" not in product
    assert "清爽質地" in product["rank_reason"]
    assert not any(word in product["rank_reason"] for word in ("熱銷", "KOL", "醫師推薦", "百分之"))
