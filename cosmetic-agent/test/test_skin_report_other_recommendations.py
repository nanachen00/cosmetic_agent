"""Report product candidates for typed and free-text skin observations."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from agents.skin_tracking.service import SkinRecordService


@pytest.fixture
def recommendation_env(monkeypatch):
    records = []
    queries = []
    products = [
        {
            "id": "safe",
            "name": "眼周保養商品",
            "is_partner": True,
            "score": 0.8,
            "ingredients_list": ["甘油"],
        }
    ]
    profile = {}

    class Store:
        def query(self, *_args):
            return list(records)

    def product_search(query, category=None, top_k=18, partner_only=False):
        queries.append((query, category, top_k, partner_only))
        if not partner_only:
            return list(products)
        matched = []
        for product in products:
            source = str(product.get("source") or "").strip().lower()
            if product.get("is_partner") or source in {"tomods", "tomods_official"}:
                matched.append({**product, "is_partner": True})
        return matched

    product_module = ModuleType("agents.product.product_agent")
    product_module.product_search = product_search
    graph_module = ModuleType("tools.search.graph_search")
    graph_module.graph_search = lambda ids, user_skin_type=None: {"ingredients": []}
    profile_module = ModuleType("database.profile_repository")
    profile_module.profile_repository = SimpleNamespace(get_profile=lambda user_id: profile)

    monkeypatch.setenv("AUTH_STORAGE_BACKEND", "neo4j")
    monkeypatch.setitem(sys.modules, "agents.product.product_agent", product_module)
    monkeypatch.setitem(sys.modules, "tools.search.graph_search", graph_module)
    monkeypatch.setitem(sys.modules, "database.profile_repository", profile_module)
    return SkinRecordService(Store()), records, queries, products, profile


def test_black_circle_tag_produces_eye_care_candidates(recommendation_env):
    service, records, queries, _, _ = recommendation_env
    records.append({
        "issue": "other", "severity": 4,
        "tag_slugs": ["periorbital_darkening"], "note": "最近黑眼圈很重",
    })

    result = service.current_recommendations("user")

    assert result["products"]
    assert "黑眼圈" in result["basis"]
    assert "眼霜" in queries[0][0]
    assert queries[0][1:] == (None, 18, False)


def test_legacy_black_circle_note_without_tag_is_supported(recommendation_env):
    service, records, queries, _, _ = recommendation_env
    records.append({"issue": "other", "severity": 3, "note": "黑眼圈很重"})

    result = service.current_recommendations("user")

    assert result["products"]
    assert "黑眼圈" in queries[0][0]


def test_other_observation_uses_its_own_label(recommendation_env):
    service, records, queries, _, _ = recommendation_env
    records.append({"issue": "other", "severity": 2,
                    "tag_slugs": ["under_eye_puffiness"], "note": "有眼袋"})

    result = service.current_recommendations("user")

    assert result["products"]
    assert "眼袋" in result["basis"]
    assert "眼周" in queries[0][0]


def test_disease_like_or_vague_other_does_not_trigger_search(recommendation_env):
    service, records, queries, _, _ = recommendation_env
    records.append({"issue": "other", "severity": 5,
                    "tag_slugs": ["stye_pattern"], "note": "針眼"})
    assert service.current_recommendations("user")["products"] == []
    records[:] = [{"issue": "other", "severity": 1, "note": "說不清楚的狀況"}]
    assert service.current_recommendations("user")["products"] == []
    assert queries == []


def test_existing_acne_search_still_works(recommendation_env):
    service, records, queries, _, _ = recommendation_env
    records.append({"issue": "acne", "severity": 2})

    result = service.current_recommendations("user")

    assert result["products"]
    assert "痘痘" in queries[0][0]


def test_allergen_exclusion_still_applies_to_other(recommendation_env):
    service, records, _, products, profile = recommendation_env
    profile["allergens"] = ["酒精"]
    records.append({"issue": "other", "severity": 4,
                    "tag_slugs": ["periorbital_darkening"], "note": "黑眼圈"})
    products[:] = [
        {"id": "excluded", "name": "含酒精", "is_partner": True,
         "score": 0.9, "ingredients_list": ["酒精"]},
        {"id": "safe", "name": "無酒精", "is_partner": True,
         "score": 0.8, "ingredients_list": ["甘油"]},
    ]

    result = service.current_recommendations("user")

    assert [product["id"] for product in result["products"]] == ["safe"]
    assert result["excluded_ingredients"] == ["酒精"]


def test_general_report_can_recommend_external_products(recommendation_env):
    service, records, queries, products, _ = recommendation_env
    records.append({"issue": "redness", "severity": 3})
    products[:] = [
        {"id": "external", "name": "外部舒緩精華", "is_partner": False,
         "score": 0.99, "ingredients_list": ["積雪草"]},
    ]

    result = service.current_recommendations("user")

    assert [item["id"] for item in result["products"]] == ["external"]
    assert result["products"][0]["is_partner"] is False
    assert result["message"] == ""
    assert queries[0][3] is False


def test_general_report_keeps_database_partner_flag(recommendation_env):
    service, records, _, products, _ = recommendation_env
    records.append({"issue": "dryness", "severity": 2})
    products[:] = [
        {"id": "tomods", "name": "三友保濕乳霜", "is_partner": False,
         "source": "tomods_official", "score": 0.8,
         "ingredients_list": ["神經醯胺"]},
    ]

    result = service.current_recommendations("user")

    assert [product["id"] for product in result["products"]] == ["tomods"]
    assert result["products"][0]["is_partner"] is False


def test_general_report_can_mix_partner_and_non_partner(recommendation_env):
    service, records, queries, products, _ = recommendation_env
    records.append({"issue": "acne", "severity": 2})
    products[:] = [
        {"id": "general", "name": "抗痘凝膠", "is_partner": False,
         "score": 0.9, "ingredients_list": ["甘油"]},
        {"id": "partner", "name": "三友抗痘乳", "is_partner": True,
         "score": 0.8, "ingredients_list": ["甘油"]},
    ]
    result = service.current_recommendations("user")
    assert {item["id"] for item in result["products"]} == {"general", "partner"}
    assert {item["is_partner"] for item in result["products"]} == {False, True}
    assert queries[0][3] is False


def test_general_report_uses_generic_empty_message(recommendation_env):
    service, records, queries, products, _ = recommendation_env
    records.append({"issue": "acne", "severity": 2})
    products[:] = []
    result = service.current_recommendations("user")
    assert result["products"] == []
    assert result["message"] == "目前沒有符合膚況與個人條件的商品候選。"
    assert queries[0][3] is False
