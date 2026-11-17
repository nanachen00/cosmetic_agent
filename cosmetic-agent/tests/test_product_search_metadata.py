"""Search branches retain catalogue evidence without manufacturing missing data."""

import re

import pytest

from agents.product import product_agent as agent


EVIDENCE_FIELDS = (
    "ingredients_inci", "inci_status", "ingredient_evidence_level",
    "ingredient_source_url", "inci_source_url", "formula_market",
    "recommendation_scope", "recommendation_limitations", "skin_types",
    "suitable_skin_types", "skin_types_source_url", "skin_types_verification_status",
    "texture", "texture_source_url",
    "texture_verification_status", "price_observed_at", "price_source_url", "currency",
    "review_count", "rating", "rating_source_url", "review_source_url",
    "review_sponsored", "is_sponsored_review",
)


def _search(branch):
    if branch == "vector":
        return agent.product_search("保濕精華", top_k=3)
    if branch == "category":
        return agent.product_search("保濕精華", category="精華液", top_k=3)
    if branch == "partner":
        return agent.partner_product_search("保濕精華", top_k=3)
    if branch == "brands":
        return agent.product_search_by_brands(["Example"], limit=3)
    return agent.find_product_by_identity("保濕精華", product_id="catalogue-1")


@pytest.mark.parametrize("branch", ["vector", "category", "partner", "brands", "identity"])
@pytest.mark.parametrize("evidence_present", [True, False])
def test_every_search_projects_raw_evidence_and_retains_unknowns(monkeypatch, branch, evidence_present):
    evidence = {key: None for key in EVIDENCE_FIELDS}
    # Currency normalization is an existing separate policy. Use a known currency
    # here; the query contract below still requires a raw nullable projection.
    evidence["currency"] = "TWD"
    if evidence_present:
        evidence.update(
            ingredients_inci=["Aqua", "Glycerin"],
            inci_status="partial",
            ingredient_evidence_level="official_highlights",
            ingredient_source_url="https://example.org/featured",
            inci_source_url="https://example.org/formula",
            formula_market="TW",
            recommendation_scope="official_featured_ingredients_only",
            recommendation_limitations="未完成完整 INCI，不能作完整過敏原排除。",
            skin_types=["乾性"],
            suitable_skin_types=["混合性"],
            skin_types_source_url="https://example.org/skin-types",
            skin_types_verification_status="verified",
            texture="水感輕盈質地",
            texture_source_url="https://example.org/texture",
            texture_verification_status="verified",
            price_observed_at="2026-11-17",
            price_source_url="https://example.org/price",
            currency="TWD",
            review_source_url="https://example.org/reviews",
            review_sponsored=True,
            is_sponsored_review=False,
        )
    # No review evidence exists for this product, even when formula evidence does.
    row = {"id": "catalogue-1", "name": "保濕精華", "brand": "Example",
           "source": "product_review", "price": None, "score": 0.9,
           "review_score": None, **evidence}
    captured = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def run(self, query, **parameters):
            captured.append((query, parameters))
            return [dict(row)]

    class Driver:
        def session(self):
            return Session()

    monkeypatch.setattr(agent, "get_query_embedding", lambda _: [0.1, 0.2])
    monkeypatch.setattr(agent, "get_driver", lambda: Driver())
    result = _search(branch)

    assert len(captured) == 1
    projection = captured[0][0].split("RETURN", 1)[1]
    for key in EVIDENCE_FIELDS:
        matches = re.findall(rf"\bnode\.{key}\s+AS\s+{key}\b", projection)
        assert len(matches) == 1, (branch, key)
        assert result[0][key] == evidence[key]
    assert "coalesce(node.review_score, node.rating) AS review_score" in projection
    assert not re.search(r"coalesce\(node\.review_score,\s*node\.rating,", projection)
    assert result[0]["review_score"] is None
    assert result[0]["review_count"] is None
    assert result[0]["rating"] is None
    assert result[0]["rating_source_url"] is None
    assert result[0]["name"] == row["name"]
    assert result[0]["score"] == 0.9
