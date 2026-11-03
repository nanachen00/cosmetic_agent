from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.skin_tracking.service as agent_skin_service
from app.api.routes.skin_records import create_skin_record_router
from app.services.skin_record_service import SkinRecordService
from tools.agent.tracking.skin_save_tool import save_skin_record


class FakeStore:
    def __init__(self):
        self.saved = []
        self.updated = None
        self.deleted = None

    def query(self, user_id, **kwargs):
        return [{"user_id": user_id, **kwargs}]

    def update(self, user_id, record_id, fields):
        self.updated = (user_id, record_id, fields)
        return True

    def delete(self, user_id, record_id):
        self.deleted = (user_id, record_id)
        return True

    def check_eligibility(self, user_id, min_days):
        return {
            "eligible": False,
            "user_id": user_id,
            "min_days": min_days,
            "consecutive_days": 0,
            "days_needed": min_days,
            "streak_dates": [],
        }


def build_service():
    store = FakeStore()
    report_calls = []

    def save_record(active_store, user_id, draft):
        active_store.saved.append((user_id, draft))
        return {"record_id": "r1"}

    def build_report(*, user_id, records, llm_client):
        report_calls.append((user_id, records, llm_client))
        return SimpleNamespace(model_dump=lambda: {"user_id": user_id})

    service = SkinRecordService(
        store_provider=lambda: store,
        save_record=save_record,
        build_report=build_report,
        render_report_text=lambda report: "report-text",
    )
    return service, store, report_calls


def test_skin_record_service_crud_and_report():
    service, store, report_calls = build_service()
    result = service.create(
        user_id="u1", issue="dryness", zone="chin", severity=3,
        note="note", products=["p1"], duration_days=2,
    )
    assert result == {"record_id": "r1"}
    assert service.list(user_id="u1", date_from=None, date_to=None)[0]["user_id"] == "u1"
    assert service.patch(user_id="u1", record_id="r1", fields={"severity": 2}) is True
    assert service.delete(user_id="u1", record_id="r1") is True
    eligibility = service.eligibility(user_id="u1")
    assert eligibility["eligible"] is True
    assert eligibility["days_needed"] == 0
    assert eligibility["min_days"] == 5
    text, report = service.build_current_report(user_id="u1")
    assert text == "report-text"
    assert report.model_dump() == {"user_id": "u1"}
    assert report_calls == [("u1", [{"user_id": "u1"}], None)]
    assert store.updated == ("u1", "r1", {"severity": 2})
    assert store.deleted == ("u1", "r1")


def test_skin_record_service_maps_frontend_payload():
    service, store, _report_calls = build_service()
    service.create_from_frontend(
        user_id="u1",
        region="左臉頰",
        conditions=["乾燥", "roughness"],
        severity=4,
        notes="乾",
        record_date="2026-10-18",
    )
    user_id, draft = store.saved[-1]
    assert user_id == "u1"
    assert draft == {
        "intent": "create",
        "issue": "dryness",
        "zone": "left_cheek",
        "severity": 4,
        "note": "乾（另有：roughness）",
        "products": [],
        "record_date": "2026-10-18",
    }



class LegacyLockedReportService:
    def eligibility(self, *, user_id):
        return {"eligible": False, "consecutive_days": 0, "days_needed": 5}

    def build_current_report(self, *, user_id):
        report = SimpleNamespace(model_dump=lambda: {"user_id": user_id, "items": []})
        return "目前沒有膚況紀錄，建立第一筆後即可看到趨勢。", report


def test_current_report_route_ignores_legacy_locked_metadata():
    app = FastAPI()
    app.include_router(create_skin_record_router(
        owner_resolver=lambda _request, user_id: user_id,
        service=LegacyLockedReportService(),
    ))
    response = TestClient(app).get(
        "/api/skin-reports/current",
        params={"user_id": "u1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["eligibility"]["eligible"] is True
    assert body["eligibility"]["days_needed"] == 0
    assert body["report"]["items"] == []


class LegacyLockedSaveStore:
    def __init__(self):
        self.eligibility_min_days = None
        self.saved_record = None

    def create(self, _user_id, record):
        self.saved_record = record
        return "r1"

    def check_eligibility(self, _user_id, _today, min_days):
        self.eligibility_min_days = min_days
        return {"eligible": False, "consecutive_days": 1, "days_needed": 4}


def test_save_skin_record_returns_unlocked_legacy_metadata():
    store = LegacyLockedSaveStore()
    result = save_skin_record(store, "u1", {
        "intent": "create",
        "issue": "dryness",
        "zone": "chin",
        "severity": 3,
        "note": "",
        "products": [],
    })
    assert result["eligibility"]["eligible"] is True
    assert result["eligibility"]["days_needed"] == 0
    assert result["eligibility"]["consecutive_days"] == 1
    assert store.eligibility_min_days == 5


def test_save_skin_record_preserves_selected_record_date():
    store = LegacyLockedSaveStore()
    save_skin_record(store, "u1", {
        "intent": "create",
        "issue": "acne",
        "zone": "t_zone",
        "severity": 2,
        "note": "",
        "products": [],
        "record_date": "2026-10-18",
    })
    assert store.saved_record["date"].isoformat() == "2026-10-18"


class LegacyLockedAgentStore:
    def check_eligibility(self, _user_id, _today, min_days):
        return {"eligible": False, "consecutive_days": 1, "days_needed": min_days - 1}

    def query(self, _user_id, _date_from, _date_to):
        return [{"record_id": "r1", "date": "2026-10-03", "issue": "dryness"}]


def test_agent_report_builds_when_legacy_metadata_says_locked(monkeypatch):
    monkeypatch.setattr(agent_skin_service, "build_report_display_notes", lambda *_args: {})
    monkeypatch.setattr(
        agent_skin_service,
        "build_report",
        lambda **kwargs: SimpleNamespace(
            model_dump=lambda **_kwargs: {"user_id": kwargs["user_id"]}
        ),
    )
    service = agent_skin_service.SkinRecordService(LegacyLockedAgentStore())
    result = service.current_report("u1")
    assert result["eligibility"]["eligible"] is True
    assert result["eligibility"]["days_needed"] == 0
    assert result["report"] == {"user_id": "u1"}
    assert result["records"][0]["record_id"] == "r1"
