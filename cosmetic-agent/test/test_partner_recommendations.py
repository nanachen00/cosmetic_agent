"""The dedicated partner endpoint must never re-label a non-partner product."""
from __future__ import annotations

import ast
import sys
import time
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agents.skin_tracking.http_api import create_router
from agents.skin_tracking.service import SkinRecordService


@pytest.fixture
def partner_env(monkeypatch):
    records = []
    products = []
    profile = {}
    searches = []
    queries = []

    class Store:
        def query(self, user_id, date_from=None, date_to=None, *_args):
            queries.append((user_id, date_from, date_to))
            return list(records)

    product_module = ModuleType("agents.product.product_agent")
    product_module.product_search = lambda *_args, **_kwargs: searches.append("legacy") or []
    product_module.partner_product_search = lambda query, top_k: (
        searches.append((query, top_k)) or list(products)
    )
    graph_module = ModuleType("tools.search.graph_search")
    graph_module.graph_search = lambda ids, user_skin_type=None: {"ingredients": []}
    profile_module = ModuleType("database.profile_repository")
    profile_module.profile_repository = SimpleNamespace(get_profile=lambda _user: profile)
    monkeypatch.setitem(sys.modules, "agents.product.product_agent", product_module)
    monkeypatch.setitem(sys.modules, "tools.search.graph_search", graph_module)
    monkeypatch.setitem(sys.modules, "database.profile_repository", profile_module)
    monkeypatch.setenv("AUTH_STORAGE_BACKEND", "neo4j")
    return SkinRecordService(Store()), records, products, profile, searches, queries


def test_strict_partner_flag_is_required_even_for_tomods_source(partner_env):
    service, records, products, _, searches, _ = partner_env
    records.append({"issue": "acne", "severity": 4})
    products.extend([
        {"id": "fake", "name": "抗痘商品", "source": "tomods_official",
         "is_partner": False, "score": 0.99},
        {"id": "real", "name": "Partner Acne Lotion", "name_zh": "三友抗痘乳",
         "is_partner": True,
         "score": 0.8, "ingredients_list": ["甘油"]},
    ])

    result = service.partner_recommendations("u1")

    assert [item["id"] for item in result["products"]] == ["real"]
    assert result["products"][0]["is_partner"] is True
    assert result["products"][0]["name"] == "三友抗痘乳"
    assert "legacy" not in searches


def test_issue_match_precedes_unrelated_high_score(partner_env):
    service, records, products, _, _, _ = partner_env
    records.append({"issue": "oiliness", "severity": 3})
    products.extend([
        {"id": "unrelated", "name": "花香香水", "is_partner": True,
         "score": 0.99, "ingredients_list": ["甘油"]},
        {"id": "matched", "name": "清爽控油乳", "is_partner": True,
         "score": 0.3, "ingredients_list": ["甘油"]},
    ])
    assert [item["id"] for item in service.partner_recommendations("u1")["products"]] == ["matched"]


def test_allergy_and_pregnancy_exclusions_apply(partner_env):
    service, records, products, profile, _, _ = partner_env
    records.append({"issue": "dryness", "severity": 2})
    profile.update({"allergens": ["酒精"], "wellnessStatus": "pregnant"})
    products.extend([
        {"id": "allergy", "name": "保濕乳", "is_partner": True,
         "score": 0.9, "ingredients_list": ["酒精"]},
        {"id": "retinol", "name": "保濕精華", "is_partner": True,
         "score": 0.8, "ingredients_list": ["retinol"]},
        {"id": "unknown", "name": "保濕霜", "is_partner": True, "score": 0.7},
        {"id": "safe", "name": "保濕乳液", "is_partner": True,
         "score": 0.6, "ingredients_list": ["甘油"]},
    ])
    result = service.partner_recommendations("u1")
    assert [item["id"] for item in result["products"]] == ["safe"]
    assert result["excluded_ingredients"] == ["酒精"]


def test_skin_type_daily_fallback_only_when_no_issue_match(partner_env):
    service, records, products, profile, _, _ = partner_env
    records.append({"issue": "acne", "severity": 2})
    profile["skinType"] = "敏感肌"
    products.extend([
        {"id": "other", "name": "日常乾性肌乳液", "is_partner": True,
         "score": 0.9, "ingredients_list": ["甘油"]},
        {"id": "fallback", "name": "敏感肌日常保養乳", "is_partner": True,
         "score": 0.6, "ingredients_list": ["甘油"]},
    ])
    result = service.partner_recommendations("u1")
    assert [item["id"] for item in result["products"]] == ["fallback"]
    assert "不代表能改善" in result["products"][0]["reason"]


def test_explicit_skin_type_mismatch_and_graph_warning_are_excluded(partner_env):
    service, records, products, profile, _, _ = partner_env
    records.append({"issue": "oiliness", "severity": 3})
    profile["skinType"] = "油性肌"
    products.extend([
        {"id": "dry-only", "name": "控油乳", "cohort": "乾性肌",
         "is_partner": True, "score": 0.9, "ingredients_list": ["甘油"]},
        {"id": "warned", "name": "控油乳", "cohort": "油性肌",
         "is_partner": True, "score": 0.8, "ingredients_list": ["刺激成分"]},
        {"id": "safe", "name": "控油乳", "cohort": "油性肌",
         "is_partner": True, "score": 0.7, "ingredients_list": ["甘油"]},
    ])
    sys.modules["tools.search.graph_search"].graph_search = lambda *_args, **_kwargs: {
        "ingredients": [], "warnings": ["刺激成分 不建議oily膚質使用"],
    }
    assert [item["id"] for item in service.partner_recommendations("u1")["products"]] == ["safe"]


def test_no_skin_type_or_partner_match_returns_empty(partner_env):
    service, records, products, _, _, _ = partner_env
    records.append({"issue": "redness", "severity": 2})
    products.append({"id": "unrelated", "name": "香水", "is_partner": True,
                     "score": 1.0, "ingredients_list": ["甘油"]})
    result = service.partner_recommendations("u1")
    assert result["products"] == []
    assert "三友商品庫" in result["message"]


def test_selected_dates_and_other_observation_are_supported(partner_env):
    service, records, products, _, searches, queries = partner_env
    records.append({"issue": "other", "severity": 3,
                    "tag_slugs": ["periorbital_darkening"], "note": "黑眼圈"})
    products.append({"id": "eye", "name": "黑眼圈眼霜", "is_partner": True,
                     "score": 0.7, "ingredients_list": ["甘油"]})
    result = service.partner_recommendations(
        "u1", date_from=date(2026, 9, 20), date_to=date(2026, 9, 22)
    )
    assert queries == [("u1", "2026-09-20", "2026-09-22")]
    assert "黑眼圈" in searches[0][0]
    assert [item["id"] for item in result["products"]] == ["eye"]


def test_partner_route_contract_and_invalid_dates():
    service = SimpleNamespace(partner_recommendations=lambda user_id, **kwargs: {
        "user_id": user_id, **kwargs,
    })
    router = create_router(
        get_user_id=lambda: "u1",
        get_tracking_service=lambda: None,
        get_record_service=lambda: service,
    )
    endpoint = next(route.endpoint for route in router.routes
                    if route.path == "/api/skin-tracking/reports/partner-recommendations")
    result = endpoint(period_days=14, limit=3, date_from=date(2026, 9, 20),
                      date_to=date(2026, 9, 22), user_id="u1")
    assert result["user_id"] == "u1"
    assert result["date_from"] == date(2026, 9, 20)
    for start, end in [
        (date(2026, 9, 20), None),
        (date(2026, 9, 22), date(2026, 9, 20)),
        (date(2026, 1, 1), date(2026, 4, 1)),
    ]:
        with pytest.raises(HTTPException) as caught:
            endpoint(period_days=14, limit=3, date_from=start,
                     date_to=end, user_id="u1")
        assert caught.value.status_code == 422


def test_catalog_query_filters_database_flag_before_limit():
    """Exercise the real query builder without importing unrelated agent services."""
    source = Path(__file__).resolve().parents[1] / "agents/product/product_agent.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "partner_product_search")
    captured = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, query, **params):
            captured.append((query, params))
            return [{"id": "partner", "is_partner": True, "score": 0.7}]

    namespace = {
        "time": time,
        "get_query_embedding": lambda _query: [0.1, 0.2],
        "observe_pipeline_stage": lambda *_args: None,
        "get_driver": lambda: SimpleNamespace(session=lambda: Session()),
        "_normalize_catalog_prices": lambda rows: rows,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    result = namespace["partner_product_search"]("黑眼圈", top_k=3)

    assert result[0]["id"] == "partner"
    query, params = captured[0]
    assert "coalesce(node.is_partner, false) = true" in query
    assert query.index("coalesce(node.is_partner, false) = true") < query.index("LIMIT $top_k")
    assert "tomods_official" not in query
    assert params["top_k"] == 3


def test_partner_http_route_accepts_dates_and_rejects_missing_pair():
    service = SimpleNamespace(partner_recommendations=lambda user_id, **kwargs: {
        "user_id": user_id,
        "date_from": kwargs["date_from"].isoformat(),
        "date_to": kwargs["date_to"].isoformat(),
        "products": [],
    })
    app = FastAPI()
    app.include_router(create_router(
        get_user_id=lambda: "u1",
        get_tracking_service=lambda: None,
        get_record_service=lambda: service,
    ))
    client = TestClient(app)
    path = "/api/skin-tracking/reports/partner-recommendations"
    response = client.get(path, params={"date_from": "2026-09-20", "date_to": "2026-09-22"})
    assert response.status_code == 200
    assert response.json()["date_from"] == "2026-09-20"
    assert client.get(path, params={"date_from": "2026-09-20"}).status_code == 422
