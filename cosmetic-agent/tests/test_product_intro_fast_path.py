import requests

from agents.product import product_agent
from agents.root.orchestrator import _format_ranked_products
from chatgpt import terra_client
from schemas.intent import IntentParseResult, IntentType, ProductCategory


def test_product_run_preserves_recommendation_without_unused_intro(monkeypatch):
    def unexpected_completion(*args, **kwargs):
        raise AssertionError("product search must not generate an unused introduction")

    # Guard the client and the former imported alias against accidental reuse.
    monkeypatch.setattr(terra_client, "generate_product_intro", unexpected_completion)
    monkeypatch.setattr(product_agent, "generate_product_intro", unexpected_completion, raising=False)
    monkeypatch.setattr(requests, "post", unexpected_completion)
    monkeypatch.delenv("PRODUCT_COPY_USE_LLM", raising=False)

    candidate = {
        "id": "hydration-demo",
        "name": "測試保濕乳液",
        "brand": "Demo",
        "category": "乳液",
        "price": 399,
        "score": 0.9,
        "is_partner": True,
        "ingredients_list": ["glycerin"],
        "source_urls": ["https://example.com/hydration-demo"],
    }
    monkeypatch.setattr(product_agent, "product_search", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(product_agent, "_enrich_partner_products", lambda *args: {
        "ingredients": [{"product_id": "hydration-demo", "ingredient_name_zh": "甘油"}],
        "warnings": [],
    })
    intent = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        product_category=ProductCategory.LOTION,
        concern=["保濕"],
        raw_query="找保濕乳液",
    )

    result = product_agent.run(intent)

    assert set(result) == {
        "products", "terra_intro", "graph_summary", "warnings", "knowledge_result",
        "total_candidates", "personalization_applied", "customer_type",
    }
    assert result["terra_intro"] == ""
    assert result["total_candidates"] == 1
    product = result["products"][0]
    assert (product["id"], product["name"], product["brand"], product["price"]) == (
        "hydration-demo", "測試保濕乳液", "Demo", 399,
    )
    assert product["ingredients"] == ["甘油"]
    assert product["sources"] == [{"url": "https://example.com/hydration-demo"}]
    assert "甘油" in product["copy"]
    assert product["recommendation_reason"]
    assert "甘油" in result["graph_summary"]

    reply = _format_ranked_products(result, {}, intent)
    assert "測試保濕乳液" in reply
    assert "399" in reply
    assert "甘油" in reply
    assert "https://example.com/hydration-demo" in reply
    # The active renderer already ignores the old generated introduction.
    assert _format_ranked_products({**result, "terra_intro": "unused intro sentinel"}, {}, intent) == reply
