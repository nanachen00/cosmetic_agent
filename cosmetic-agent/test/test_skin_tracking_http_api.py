"""Skin Tracking HTTP contract tests; services are isolated from models and databases."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.skin_tracking.extraction import SkinExtractionProviderError
from agents.skin_tracking.http_api import create_router


class FakeTrackingService:
    def __init__(self):
        self.send_calls = []
        self.session_result = None
        self.send_error = None

    def send(self, user_id, session_id, message):
        self.send_calls.append((user_id, session_id, message))
        if self.send_error:
            raise self.send_error
        return {
            "reply": "請描述今天的膚況。",
            "quick_replies": [],
            "stage": "skin_tracking_collect",
            "done": False,
            "data": None,
        }

    def get_session(self, user_id, session_id):
        return self.session_result


class FakeRecordService:
    def __init__(self):
        self.list_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.update_result = True
        self.delete_result = True

    def list_records(self, user_id, date_from, date_to, issue, zone):
        self.list_calls.append((user_id, date_from, date_to, issue, zone))
        return [{"record_id": "record-1", "record_status": "recorded"}]

    def update_record(self, user_id, record_id, fields):
        self.update_calls.append((user_id, record_id, fields))
        return self.update_result

    def delete_record(self, user_id, record_id):
        self.delete_calls.append((user_id, record_id))
        return self.delete_result

    def current_report(self, user_id, period_days):
        return {"user_id": user_id, "period_days": period_days, "report": None}


def make_client():
    tracking = FakeTrackingService()
    records = FakeRecordService()
    app = FastAPI()
    app.include_router(
        create_router(
            get_user_id=lambda: "user-1",
            get_tracking_service=lambda: tracking,
            get_record_service=lambda: records,
        )
    )
    return TestClient(app, raise_server_exceptions=False), tracking, records


def test_message_trims_text_and_rejects_whitespace_only_input():
    client, tracking, _records = make_client()

    response = client.post(
        "/api/skin-tracking/message",
        json={"session_id": " session-1 ", "message": "  下巴今天長痘痘  "},
    )
    assert response.status_code == 200
    assert tracking.send_calls == [("user-1", "session-1", "下巴今天長痘痘")]

    response = client.post(
        "/api/skin-tracking/message",
        json={"session_id": "session-1", "message": "   "},
    )
    assert response.status_code == 422
    assert len(tracking.send_calls) == 1


def test_model_failure_returns_sanitized_service_unavailable():
    client, tracking, _records = make_client()
    tracking.send_error = SkinExtractionProviderError(
        "provider secret and internal request details"
    )

    response = client.post(
        "/api/skin-tracking/message",
        json={"session_id": "session-1", "message": "今天很乾"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Skin Tracking is temporarily unavailable"}
    assert "provider secret" not in response.text


def test_expired_session_returns_gone():
    client, _tracking, _records = make_client()
    response = client.get("/api/skin-tracking/sessions/missing-session")
    assert response.status_code == 410
    assert response.json() == {"detail": "Session expired"}


def test_record_filters_are_validated_and_forwarded_as_domain_values():
    client, _tracking, records = make_client()

    response = client.get(
        "/api/skin-tracking/records",
        params={
            "date_from": "2026-08-01",
            "date_to": "2026-08-18",
            "issue": "acne",
            "zone": "chin",
        },
    )
    assert response.status_code == 200
    assert records.list_calls == [
        ("user-1", "2026-08-01", "2026-08-18", "acne", "chin")
    ]

    invalid = client.get(
        "/api/skin-tracking/records",
        params={"date_from": "not-a-date", "zone": "whole_face"},
    )
    assert invalid.status_code == 422
    assert len(records.list_calls) == 1


def test_patch_validates_fields_and_reports_missing_record():
    client, _tracking, records = make_client()

    response = client.patch(
        "/api/skin-tracking/records/record-1",
        json={"severity": 4, "zone": "left_cheek", "onset_date": "2026-08-17"},
    )
    assert response.status_code == 200
    assert records.update_calls == [
        (
            "user-1",
            "record-1",
            {"severity": 4, "zone": "left_cheek", "onset_date": "2026-08-17"},
        )
    ]

    assert client.patch("/api/skin-tracking/records/record-1", json={}).status_code == 422
    assert client.patch(
        "/api/skin-tracking/records/record-1",
        json={"date": "yesterday"},
    ).status_code == 422

    records.update_result = False
    missing = client.patch(
        "/api/skin-tracking/records/missing",
        json={"severity": 2},
    )
    assert missing.status_code == 404


def test_delete_and_report_web_states():
    client, _tracking, records = make_client()
    records.delete_result = False

    assert client.delete("/api/skin-tracking/records/missing").status_code == 404
    assert client.get(
        "/api/skin-tracking/reports/current",
        params={"period_days": 14},
    ).status_code == 200
    assert client.get(
        "/api/skin-tracking/reports/current",
        params={"period_days": 4},
    ).status_code == 422


if __name__ == "__main__":
    test_message_trims_text_and_rejects_whitespace_only_input()
    test_model_failure_returns_sanitized_service_unavailable()
    test_expired_session_returns_gone()
    test_record_filters_are_validated_and_forwarded_as_domain_values()
    test_patch_validates_fields_and_reports_missing_record()
    test_delete_and_report_web_states()
    print("Skin Tracking HTTP API tests passed")
