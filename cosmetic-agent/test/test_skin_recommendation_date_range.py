"""Date-range contracts for the skin report recommendation endpoint."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.skin_tracking.http_api import create_router
from agents.skin_tracking.service import SkinRecordService


class RangeStore:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def query(self, user_id, date_from=None, date_to=None, issue=None, zone=None):
        self.calls.append((user_id, date_from, date_to))
        return [
            record
            for record in self.records
            if record["user_id"] == user_id
            and (date_from is None or date_from <= record["date"])
            and (date_to is None or record["date"] <= date_to)
        ]


def _stub_module(monkeypatch, name, **values):
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_recommendations_use_only_selected_dates(monkeypatch):
    store = RangeStore([
        {"user_id": "u1", "date": "2026-09-19", "issue": "acne", "severity": 5},
        {"user_id": "u1", "date": "2026-09-21", "issue": "dryness", "severity": 1},
    ])
    searched = []
    _stub_module(
        monkeypatch,
        "agents.product.product_agent",
        product_search=lambda query, category, top_k: (
            searched.append(query)
            or [{
                "id": "p1", "name": "保濕乳", "brand": "示例",
                "category": "保濕", "score": 1, "ingredients_list": [],
            }]
        ),
    )
    _stub_module(
        monkeypatch,
        "tools.search.graph_search",
        graph_search=lambda product_ids, user_skin_type: {"ingredients": []},
    )
    _stub_module(
        monkeypatch,
        "database.postgres.repositories.profile",
        postgres_profile_repository=SimpleNamespace(get_profile=lambda user_id: {}),
    )
    monkeypatch.setenv("AUTH_STORAGE_BACKEND", "postgres")

    result = SkinRecordService(store).current_recommendations(
        "u1", date_from=date(2026, 9, 20), date_to=date(2026, 9, 22)
    )

    assert store.calls == [("u1", "2026-09-20", "2026-09-22")]
    assert len(searched) == 1
    assert "乾燥" in searched[0]
    assert "痘痘" not in searched[0]
    assert "2026/09/20～2026/09/22" in result["basis"]
    assert result["products"][0]["id"] == "p1"


def test_legacy_period_days_still_uses_recent_days():
    store = RangeStore([])
    result = SkinRecordService(store).current_recommendations(
        "u1", period_days=14
    )
    assert store.calls == [(
        "u1",
        str(date.today() - timedelta(days=13)),
        str(date.today()),
    )]
    assert result["products"] == []


def _recommendation_endpoint():
    service = SimpleNamespace(current_recommendations=lambda user_id, **kwargs: {
        "user_id": user_id, **kwargs
    })
    router = create_router(
        get_user_id=lambda request: "u1",
        get_tracking_service=lambda: None,
        get_record_service=lambda: service,
    )
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/skin-tracking/reports/recommendations"
    )


def test_route_passes_selected_dates():
    endpoint = _recommendation_endpoint()
    result = endpoint(
        period_days=14, limit=3,
        date_from=date(2026, 9, 20),
        date_to=date(2026, 9, 22),
        user_id="u1",
    )
    assert result["date_from"] == date(2026, 9, 20)
    assert result["date_to"] == date(2026, 9, 22)


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2026, 9, 20), None),
        (None, date(2026, 9, 22)),
        (date(2026, 9, 22), date(2026, 9, 20)),
        (date(2026, 1, 1), date(2026, 4, 1)),
    ],
)
def test_route_rejects_invalid_selected_dates(start, end):
    endpoint = _recommendation_endpoint()
    with pytest.raises(HTTPException) as caught:
        endpoint(
            period_days=14, limit=3,
            date_from=start, date_to=end, user_id="u1"
        )
    assert caught.value.status_code == 422
