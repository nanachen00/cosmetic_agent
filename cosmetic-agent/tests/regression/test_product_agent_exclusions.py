from agents.product.product_agent import (
    _product_rank_key,
    filter_unseen_products,
    product_stable_key,
)


def test_production_candidates_exclude_seen_ids_and_return_next_batch():
    candidates = [
        {"id": product_id, "name": f"商品 {product_id}"}
        for product_id in "ABCDEFGHIJ"
    ]
    unseen = filter_unseen_products(candidates, set("ABCDE"))
    assert [product_stable_key(product) for product in unseen[:5]] == list("FGHIJ")


def test_product_stable_key_supports_brand_search_shape_and_name_fallback():
    assert product_stable_key({"id": "brand-A", "name": "品牌商品"}) == "brand-A"
    assert product_stable_key({"_id": "mongo-A", "name": "商品"}) == "mongo-A"
    assert product_stable_key({"name": "  Hydrating   Serum  "}) == "name:hydrating serum"


def test_partner_status_does_not_outrank_relevance():
    partner = {
        "id": "partner",
        "is_partner": True,
        "concern_hits": 0,
        "score": 0.40,
        "price": 100,
    }
    relevant = {
        "id": "regular",
        "is_partner": False,
        "concern_hits": 2,
        "score": 0.90,
        "price": 100,
    }
    ranked = sorted(
        [partner, relevant],
        key=lambda product: _product_rank_key(product, set()),
    )
    assert [product["id"] for product in ranked] == ["regular", "partner"]


def test_partner_status_only_breaks_an_exact_relevance_tie():
    common = {"concern_hits": 1, "score": 0.80, "price": 100}
    partner = {"id": "partner", "is_partner": True, **common}
    regular = {"id": "regular", "is_partner": False, **common}
    ranked = sorted(
        [regular, partner],
        key=lambda product: _product_rank_key(product, set()),
    )
    assert [product["id"] for product in ranked] == ["partner", "regular"]
