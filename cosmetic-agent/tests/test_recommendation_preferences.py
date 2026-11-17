from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api.routes.recommendation_preferences import (
    create_recommendation_preferences_router,
)
from database.postgres.repositories import recommendation_preferences as repository_module
from database.postgres.repositories.recommendation_preferences import (
    RecommendationPreferenceConflict,
    RecommendationPreferenceRepository,
)
from tools.user import user_analysis_context_bridge as bridge_module
from tools.user.user_analysis_context_bridge import UserAnalysisContextBridge


EMPTY = {
    "budget_max": None,
    "preferred_texture": None,
    "preferred_brands": [],
    "avoided_brands": [],
    "updated_at": None,
}


class MemoryRepository:
    def __init__(self):
        self.rows = {}

    def get(self, user_id):
        return dict(self.rows.get(user_id, EMPTY))

    def patch(self, user_id, changes):
        row = self.get(user_id)
        row.update(changes)
        preferred = {value.casefold() for value in row["preferred_brands"]}
        avoided = {value.casefold() for value in row["avoided_brands"]}
        if preferred & avoided:
            raise RecommendationPreferenceConflict("同一品牌不能同時設為喜歡與避用")
        row["updated_at"] = "2026-11-17T12:00:00+00:00"
        self.rows[user_id] = row
        return dict(row)

    def reset(self, user_id):
        self.rows[user_id] = dict(EMPTY)
        return dict(EMPTY)


def client(repository=None):
    app = FastAPI()
    repository = repository or MemoryRepository()

    def require_auth(request: Request):
        user_id = request.headers.get("x-test-user", "")
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return {"user_id": user_id}

    app.include_router(
        create_recommendation_preferences_router(
            require_auth_session=require_auth,
            repository=repository,
        )
    )
    return TestClient(app), repository


def test_preferences_require_session_and_default_to_empty():
    http, _ = client()
    assert http.get("/api/user/preferences").status_code == 401
    response = http.get("/api/user/preferences", headers={"x-test-user": "user-a"})
    assert response.status_code == 200
    assert response.json()["preferences"] == EMPTY


def test_patch_distinguishes_omitted_clear_and_replace_and_isolates_users():
    http, _ = client()
    a = {"x-test-user": "user-a"}
    b = {"x-test-user": "user-b"}
    first = http.patch(
        "/api/user/preferences",
        headers=a,
        json={
            "budget_max": 1200,
            "preferred_texture": "light",
            "preferred_brands": [" CeraVe ", "cerave", "理膚寶水"],
            "avoided_brands": ["Brand Z"],
        },
    )
    assert first.status_code == 200
    assert first.json()["preferences"]["preferred_brands"] == ["CeraVe", "理膚寶水"]

    omitted = http.patch(
        "/api/user/preferences", headers=a, json={"budget_max": 900}
    ).json()["preferences"]
    assert omitted["preferred_texture"] == "light"
    assert omitted["preferred_brands"] == ["CeraVe", "理膚寶水"]

    cleared = http.patch(
        "/api/user/preferences",
        headers=a,
        json={"preferred_texture": None, "preferred_brands": []},
    ).json()["preferences"]
    assert cleared["preferred_texture"] is None
    assert cleared["preferred_brands"] == []
    assert http.get("/api/user/preferences", headers=b).json()["preferences"] == EMPTY


def test_patch_rejects_invalid_budget_brand_overlap_and_excess_items():
    http, _ = client()
    headers = {"x-test-user": "user-a"}
    assert http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"user_id": "user-b", "budget_max": 500},
    ).status_code == 422
    assert http.patch(
        "/api/user/preferences", headers=headers, json={"budget_max": True}
    ).status_code == 422
    assert http.patch(
        "/api/user/preferences", headers=headers, json={"budget_max": -1}
    ).status_code == 422
    assert http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"preferred_brands": ["CeraVe"], "avoided_brands": ["cerave"]},
    ).status_code == 422
    assert http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"preferred_brands": [f"favorite-{index}" for index in range(6)]},
    ).status_code == 422
    assert http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"preferred_brands": [f"brand-{index}" for index in range(11)]},
    ).status_code == 422


def test_patch_detects_conflict_with_saved_other_list_and_delete_resets():
    http, _ = client()
    headers = {"x-test-user": "user-a"}
    assert http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"avoided_brands": ["CeraVe"]},
    ).status_code == 200
    conflict = http.patch(
        "/api/user/preferences",
        headers=headers,
        json={"preferred_brands": ["cerave"]},
    )
    assert conflict.status_code == 422
    reset = http.delete("/api/user/preferences", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["preferences"] == EMPTY


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.row = dict(row)
        self.queries = []

    def execute(self, query, params):
        self.queries.append(query)
        if "INSERT INTO user_preferences" not in query:
            return Result(dict(self.row))
        if len(params) == 4:
            _, budget_min, budget_max, data = params
            self.row.update(
                budget_min=budget_min,
                budget_max=budget_max,
                preferences_data=data,
            )
        else:
            _, data = params
            self.row.update(
                budget_min=None,
                budget_max=None,
                preferences_data=data,
            )
        self.row["updated_at"] = datetime(2026, 11, 17, tzinfo=timezone.utc)
        return Result(dict(self.row))


def test_repository_preserves_unmanaged_data_and_removes_legacy_texture(monkeypatch):
    connection = FakeConnection(
        {
            "budget_min": 500,
            "budget_max": Decimal("1800"),
            "preferences_data": {
                "preferred_effects": ["保濕"],
                "texture_preferences": ["rich"],
                "preferred_brands": ["Old"],
                "avoided_brands": [],
            },
            "updated_at": None,
        }
    )

    @contextmanager
    def fake_postgres():
        yield connection

    monkeypatch.setattr(repository_module, "postgres_connection", fake_postgres)
    monkeypatch.setattr(repository_module, "postgres_jsonb", lambda value: value)
    repository = RecommendationPreferenceRepository()
    result = repository.patch(
        "11111111-1111-4111-8111-111111111111",
        {
            "budget_max": None,
            "preferred_texture": None,
            "preferred_brands": [],
        },
    )
    assert result["budget_max"] is None
    assert result["preferred_texture"] is None
    assert result["preferred_brands"] == []
    assert connection.row["preferences_data"]["preferred_effects"] == ["保濕"]
    assert "texture_preferences" not in connection.row["preferences_data"]
    assert connection.row["preferences_data"]["preferred_textures"] == []
    assert any("FOR UPDATE OF u" in query for query in connection.queries)


class BridgeCursor:
    def __init__(self, rows, queries):
        self.rows = iter(rows)
        self.queries = queries
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, _params):
        self.queries.append(query)
        self.current = next(self.rows)

    def fetchone(self):
        return self.current


class BridgeConnection:
    def __init__(self, rows, queries):
        self.cursor_instance = BridgeCursor(rows, queries)

    def cursor(self):
        return self.cursor_instance


def test_bridge_reads_preferences_without_profile_and_respects_explicit_empty(monkeypatch):
    queries = []
    rows = [
        {
            "profile_data": None,
            "preferred_categories": [],
            "disliked_ingredients": [],
            "budget_min": None,
            "budget_max": 1200,
            "preferences_data": {
                "preferred_brands": ["DB Brand"],
                "avoided_brands": ["Avoid"],
                "preferred_textures": [],
                "texture_preferences": ["rich"],
            },
        },
        {},
        {"event_count": 0},
    ]

    @contextmanager
    def fake_postgres():
        yield BridgeConnection(rows, queries)

    monkeypatch.setattr(bridge_module, "postgres_connection", fake_postgres)
    bridge = UserAnalysisContextBridge(
        "11111111-1111-4111-8111-111111111111"
    )
    context = bridge._read_context(
        {"preferred_brands": [], "budget_max": None}
    )
    assert context["preferred_brands"] == []
    assert context["preferred_textures"] == []
    assert context["budget_max"] is None
    assert any("FROM users u" in query for query in queries)
