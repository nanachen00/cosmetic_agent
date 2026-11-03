from agents.product import product_agent
from app.services.product_review_service import _popularity_metadata
from tools.agent.intent_parser import _fast_parse_obvious_product_query


def test_efficacy_outranks_popularity():
    proven = {
        "id": "proven",
        "efficacy_score": 6,
        "has_ingredient_evidence": True,
        "popularity_score": 0.0,
        "score": 0.5,
    }
    merely_popular = {
        "id": "popular",
        "efficacy_score": 1,
        "has_ingredient_evidence": False,
        "popularity_score": 1.0,
        "score": 0.99,
    }
    assert product_agent._product_rank_key(proven, set()) < product_agent._product_rank_key(
        merely_popular, set()
    )


def test_popularity_is_bounded_and_auditable():
    metadata = _popularity_metadata(
        {
            "cohort": "trending",
            "evidence": [
                {"rank": 1, "source_name": "榜單 A", "context": "精華類第 1"},
                {"rank": 3, "source_name": "榜單 B", "context": "年度人氣"},
            ],
        }
    )
    assert 0 < metadata["popularity_score"] <= 1
    assert metadata["best_evidence_rank"] == 1
    assert metadata["evidence_count"] == 2
    assert metadata["trend_evidence"][0].startswith("榜單 A")


def test_unreviewed_legacy_fallback_does_not_activate_admin_candidates():
    source = product_agent.__file__
    text = open(source, encoding="utf-8").read()
    assert "node.source_type = 'admin_approved' THEN false ELSE true" in text
    assert "coalesce(node.is_active, true) = true" in text


def test_fast_product_intent_keeps_brightening_concern():
    intent = _fast_parse_obvious_product_query("幫我推薦適合暗沉的美白精華")
    assert intent is not None
    assert "美白" in intent.concern
    assert intent.product_category.value == "精華液"


def test_trend_rank_requires_auditable_evidence():
    evidenced = {
        "id": "evidenced",
        "efficacy_score": 1,
        "has_ingredient_evidence": False,
        "popularity_score": 0.6,
        "evidence_count": 2,
        "trend_evidence": ["榜單 A：年度人氣"],
        "score": 0.5,
    }
    unverified = {
        "id": "unverified",
        "efficacy_score": 6,
        "has_ingredient_evidence": True,
        "popularity_score": 0.99,
        "evidence_count": 0,
        "trend_evidence": [],
        "score": 0.99,
    }
    assert product_agent._product_rank_key(
        evidenced, set(), trend_first=True
    ) < product_agent._product_rank_key(unverified, set(), trend_first=True)
