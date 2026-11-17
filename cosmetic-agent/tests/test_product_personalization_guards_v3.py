"""Offline contract tests: all candidate sources use the same eligibility gates."""
from copy import deepcopy

import pytest

from agents.product import product_agent
from app.services.product_candidate_guards import full_inci_for_screening, brand_matches
from schemas.intent import IntentParseResult, IntentType, ProductCategory


def candidate(product_id="safe", **overrides):
    row = {
        "id": product_id, "name": f"{product_id} 保濕乳液", "brand": "Demo", "category": "乳液",
        "price": 250, "currency": "TWD", "score": .8, "is_partner": True,
        "ingredients_list": ["Glycerin"], "ingredients_inci": ["Aqua", "Glycerin", "Squalane"],
        "ingredient_evidence_level": "full_inci", "inci_status": "full", "recommendation_scope": "full_formula",
        "inci_source_url": "https://example.test/inci", "ingredient_source_url": "https://example.test/inci",
        "formula_market": "TW", "source_urls": [f"https://example.test/{product_id}"],
    }
    row.update(overrides)
    return row


def intent(**overrides):
    values = dict(intent_type=IntentType.FIND_PRODUCT, product_category=ProductCategory.LOTION,
                  concern=["保濕"], raw_query="推薦保濕乳液")
    values.update(overrides)
    return IntentParseResult(**values)


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setenv("PRODUCT_COPY_USE_LLM", "0")
    monkeypatch.setenv("PRODUCT_INGREDIENT_EXTRACTOR", "deterministic")
    monkeypatch.setattr(product_agent, "get_driver", lambda: pytest.fail("Offline gate tests must not query a DB"))
    monkeypatch.setattr(product_agent._terra_client, "complete", lambda **kwargs: pytest.fail("Offline gate tests must not call a model"))
    monkeypatch.setattr(product_agent, "_enrich_partner_products", lambda *args: {"ingredients": [], "warnings": [], "product_suits": []})

    def configure(local, web=None):
        monkeypatch.setattr(product_agent, "product_search", lambda *args, **kwargs: deepcopy(local))
        monkeypatch.setattr(product_agent, "product_search_by_brands", lambda *args, **kwargs: deepcopy(local))
        def search(*args, **kwargs):
            if web is None:
                pytest.fail("Unexpected web lookup")
            return {"products": deepcopy(web), "warnings": [], "source": "web"}
        monkeypatch.setattr("tools.search.product_web_search.search_named_product", search)
    return configure


def test_profile_budget_is_hard_without_repeating_budget_words(pipeline):
    pipeline([candidate(), candidate("over", price=900), candidate("unknown", price=None),
              candidate("nan", price=float("nan")), candidate("foreign", price=20, currency="USD")])
    result = product_agent.run(intent(), preference_context={"budget_max": 300})
    assert [row["id"] for row in result["products"]] == ["safe"]
    assert result["products"][0]["over_budget"] is False


def test_explicit_budget_overrides_saved_default_and_unlimited_is_respected(pipeline):
    pipeline([candidate(price=450)])
    assert product_agent.run(intent(budget_range="500"), preference_context={"budget_max": 300})["products"]
    assert product_agent.run(intent(raw_query="預算不限，推薦保濕乳液"), preference_context={"budget_max": 300})["products"]
    assert product_agent.run(intent(raw_query="預算不限，推薦保濕乳液", budget_range="300"), preference_context={"budget_max": 300})["products"]


@pytest.mark.parametrize("web", [False, True])
def test_all_sources_share_exclusions_avoided_brands_budget_and_full_inci(pipeline, web):
    rows = [candidate("seen"), candidate("blocked-brand", brand="DemoDanger"), candidate("over", price=600),
            candidate("unknown-price", price=None), candidate("highlights", ingredient_evidence_level="official_highlights", inci_status="missing"),
            candidate("allergen", ingredients_inci=["Aqua", "Glycerin", "Parfum"]), candidate()]
    if web:
        rows = [{**row, "is_web_result": True, "is_partner": False} for row in rows]
        pipeline([], rows)
    else:
        pipeline(rows)
    original = deepcopy(rows)
    result = product_agent.run(intent(brand_preference=["Demo"]), disliked_ingredients=["香料"],
                               preference_context={"budget_max": 300, "avoided_brands": ["DemoDanger"]}, excluded_product_ids=["seen"])
    assert [row["id"] for row in result["products"]] == ["safe"]
    assert rows == original, "Eligibility checks do not rewrite catalogue/source records"
    assert result["products"][0]["sources"] == [{"url": "https://example.test/safe"}]


def test_web_brand_and_category_cannot_escape_constraints(pipeline):
    pipeline([], [candidate("other-brand", brand="Other", is_web_result=True),
                  candidate("wrong-category", category="洗面乳", is_web_result=True)])
    result = product_agent.run(intent(brand_preference=["Demo"]))
    assert result["products"] == []


def test_trend_web_page_is_not_itself_popularity_evidence(pipeline):
    pipeline([], [candidate(is_web_result=True)])
    assert product_agent.run(intent(raw_query="熱門保濕乳液"))["products"] == []
    pipeline([], [candidate(is_web_result=True, trend_evidence=["來源榜單：保濕類第 1"], evidence_count=1)])
    assert len(product_agent.run(intent(raw_query="熱門保濕乳液"))["products"]) == 1


def test_partial_highlights_and_unknown_formula_never_satisfy_allergen_exclusion(pipeline):
    pipeline([candidate("highlights", ingredient_evidence_level="official_highlights"),
              candidate("missing-source", inci_source_url="", ingredient_source_url=""),
              candidate("missing-market", formula_market="unknown"),
              candidate("missing-list", ingredients_inci=[]),
              candidate("fragrance", ingredients_inci=["Aqua", "Glycerin", "Parfum"])])
    assert product_agent.run(intent(), disliked_ingredients=["Fragrance"])["products"] == []


@pytest.mark.parametrize("allergen", ["酒精", "Alcohol", "Ethanol"])
def test_declared_alcohol_does_not_confuse_fatty_alcohol(pipeline, allergen):
    pipeline([candidate("ethanol", ingredients_inci=["Aqua", "Alcohol", "Glycerin"]),
              candidate("fatty", ingredients_inci=["Aqua", "Cetearyl Alcohol", "Glycerin"])])
    result = product_agent.run(intent(), disliked_ingredients=[allergen])
    assert [row["id"] for row in result["products"]] == ["fatty"]


def test_verified_single_ingredient_formula_is_not_rejected_by_an_arbitrary_count():
    row = candidate(ingredients_inci=["Petrolatum"])
    assert full_inci_for_screening(row) == ["Petrolatum"]
    row["inci_source_url"] = row["ingredient_source_url"] = "http://"
    assert full_inci_for_screening(row) is None


def test_two_character_chinese_brand_exclusion_matches_bilingual_label():
    assert brand_matches('Avene 雅漾', '雅漾')
    assert not brand_matches('BrandA', 'A')


@pytest.mark.parametrize('source', ['https://example.test:bad/inci', 'https://example.test/\nformula', 'https://user:secret@example.test/inci'])
def test_malformed_formula_provenance_does_not_satisfy_allergen_guard(source):
    assert full_inci_for_screening(candidate(inci_source_url=source, ingredient_source_url=source)) is None


def test_graph_matching_uses_real_product_suits_contract(pipeline, monkeypatch):
    pipeline([candidate("graph-match"), candidate("plain")])
    monkeypatch.setattr(product_agent, "_enrich_partner_products", lambda *args: {
        "ingredients": [], "warnings": [],
        "suitable_concerns": [{"ingredient": "甘油", "suitable_concerns": ["保濕"]}],
        "product_suits": [{"product_id": "graph-match", "product_name": "graph-match 保濕乳液", "suits": ["保濕"]}],
    })
    result = product_agent.run(intent())
    by_id = {row["id"]: row for row in result["products"]}
    assert by_id["graph-match"]["efficacy_score"] == by_id["plain"]["efficacy_score"] + 5
    assert "知識圖譜標示符合保濕需求" in by_id["graph-match"]["efficacy_reasons"]


def test_existing_metadata_and_limitations_survive_without_fabrication(pipeline):
    row = candidate(recommendation_limitations="僅對已核對的台灣版配方有效。", limitations=["價格觀察日後可能變動。"],
                    price_observed_at="2026-11-17", source_url="https://example.test/product")
    pipeline([row])
    product = product_agent.run(intent())["products"][0]
    for key in ("ingredient_evidence_level", "inci_status", "recommendation_scope", "recommendation_limitations",
                "limitations", "price_observed_at", "source_url", "currency", "formula_market"):
        assert product[key] == row[key]
    plain = candidate()
    for key in ("ingredient_evidence_level", "inci_status", "recommendation_scope", "formula_market"):
        plain.pop(key)
    pipeline([plain])
    product = product_agent.run(intent())["products"][0]
    assert "price_observed_at" not in product
    assert "inci_status" not in product
    assert "recommendation_scope" not in product


def test_current_conditions_reach_scorer_and_copy_cannot_replace_evidence(pipeline, monkeypatch):
    pipeline([candidate()])
    captured = {}
    def score(products, context, *args, **kwargs):
        captured.update(context)
        for product in products:
            product.update(personalization_score=.8, personalization_policy_version="test-v3",
                           personalization_reasons=["目前條件有可核對的成分支持。"], personalization_missing=["質地資料"],
                           personalization_components={"relevance":1}, personalization_weights={"relevance":.35})
        return True
    def overwrite(products, *args, **kwargs):
        for product in products:
            product["personalization_reasons"].clear()
            product["personalization_policy_version"] = "untrusted-copy"
        return products
    monkeypatch.setattr(product_agent, "_apply_personalization_scores", score)
    monkeypatch.setattr(product_agent, "_batch_generate_rank_content", overwrite)
    result = product_agent.run(intent(budget_range="300", texture_preference="清爽"), preference_context={"preferred_brands":["Demo"]})
    assert captured["current_concerns"] == ["保濕"]
    assert captured["current_texture"] == "清爽"
    assert captured["current_budget_max"] == 300
    assert "current_skin_type" in captured
    product = result["products"][0]
    assert product["personalization_policy_version"] == "test-v3"
    assert product["personalization_reasons"] == ["目前條件有可核對的成分支持。"]
