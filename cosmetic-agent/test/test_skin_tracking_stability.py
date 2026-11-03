"""Feature 1 tests for explicit and confirmed skin stability."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.skin_tracking.extraction import SkinExtractionResult
from agents.skin_tracking.service import SkinRecordService
from agents.skin_tracking.tracking_agent import SkinTrackingAgent
from schemas.tracking_schema import RecordDraft
from tools.agent.tracking.skin_extract_tool import merge_extracted_drafts
from tools.agent.tracking.skin_record_tool import SkinRecordStore


class InMemoryStore:
    def __init__(self):
        self.records = {}

    def create(self, user_id, record):
        record_id = str(uuid.uuid4())
        self.records[record_id] = {"record_id": record_id, "user_id": user_id, **record}
        return record_id

    def query(self, user_id, *_args, **_kwargs):
        return [record for record in self.records.values() if record["user_id"] == user_id]

    def update(self, user_id, record_id, fields):
        record = self.records.get(record_id)
        if not record or record["user_id"] != user_id:
            return False
        record.update(fields)
        return True

    def delete(self, user_id, record_id):
        record = self.records.get(record_id)
        if not record or record["user_id"] != user_id:
            return False
        del self.records[record_id]
        return True

    def check_eligibility(self, _user_id, _today=None, min_days=5):
        return {
            "eligible": False,
            "consecutive_days": 1,
            "days_needed": min_days - 1,
            "streak_dates": [str(date.today())],
        }


class CompleteRecordExtractor:
    """Returns one complete draft with the requested message-local stability signal."""

    def __init__(self, stability_signal=None):
        self.stability_signal = stability_signal

    def extract(self, _message, *, today):
        record = {
            "intent": "create",
            "issue": "acne",
            "severity": 2,
            "zone": "chin",
            "onset_date": today.isoformat(),
            "products": ["保濕精華"],
            "stability_signal": self.stability_signal,
        }
        return SkinExtractionResult.model_validate({"records": [record]})


def _saved_record(store):
    assert len(store.records) == 1
    return next(iter(store.records.values()))


def test_explicit_stability_is_user_reported_without_extra_question():
    store = InMemoryStore()
    agent = SkinTrackingAgent(CompleteRecordExtractor("stable_explicit"), store)

    confirm_card = agent.handle("今天下巴痘痘維持穩定，2 分", "u1")
    assert confirm_card["stage"] == "skin_tracking_confirm"
    assert "穩定" in confirm_card["reply"]

    completed = agent.handle("確認", "u1", confirm_card["session"])
    record = _saved_record(store)
    assert completed["done"] is True
    assert record["record_status"] == "stable"
    assert record["stability_source"] == "user_reported"


def test_ambiguous_stability_requires_a_separate_confirmation():
    store = InMemoryStore()
    agent = SkinTrackingAgent(CompleteRecordExtractor("stable_ambiguous"), store)

    question = agent.handle("今天狀況差不多", "u1")
    assert question["stage"] == "skin_tracking_stability_confirm"
    assert not store.records

    confirm_card = agent.handle("確認", "u1", question["session"])
    assert confirm_card["stage"] == "skin_tracking_confirm"
    assert not store.records

    completed = agent.handle("確認", "u1", confirm_card["session"])
    record = _saved_record(store)
    assert completed["done"] is True
    assert record["record_status"] == "stable"
    assert record["stability_source"] == "agent_confirmed"


def test_unconfirmed_ambiguous_stability_stays_recorded():
    store = InMemoryStore()
    agent = SkinTrackingAgent(CompleteRecordExtractor("stable_ambiguous"), store)

    question = agent.handle("今天狀況還好", "u1")
    confirm_card = agent.handle("先只記錄", "u1", question["session"])
    assert confirm_card["stage"] == "skin_tracking_confirm"

    agent.handle("確認", "u1", confirm_card["session"])
    record = _saved_record(store)
    assert record["record_status"] == "recorded"
    assert record["stability_source"] is None


def test_general_record_confirmation_does_not_imply_stability():
    store = InMemoryStore()
    agent = SkinTrackingAgent(CompleteRecordExtractor(), store)

    confirm_card = agent.handle("今天下巴長痘痘，2 分", "u1")
    agent.handle("確認", "u1", confirm_card["session"])

    record = _saved_record(store)
    assert record["record_status"] == "recorded"
    assert record["stability_source"] is None


def test_old_records_are_exposed_as_recorded():
    store = InMemoryStore()
    record_id = store.create(
        "u1",
        {
            "date": date.today(),
            "issue": "acne",
            "severity": 2,
            "zone": "chin",
            "products": [],
        },
    )

    records = SkinRecordService(store).list_records("u1")
    assert records[0]["record_id"] == record_id
    assert records[0]["record_status"] == "recorded"
    assert records[0]["stability_source"] is None


def test_not_stable_signal_clears_previous_stability():
    current = [
        RecordDraft(
            issue="acne",
            severity=2,
            zone="chin",
            duration_days=1,
            record_status="stable",
            stability_source="user_reported",
        ).model_dump(mode="json")
    ]
    extracted = SkinExtractionResult.model_validate(
        {"records": [{"stability_signal": "not_stable"}]}
    )

    merged = merge_extracted_drafts(current, extracted.records)
    assert merged[0]["record_status"] == "recorded"
    assert merged[0]["stability_source"] is None


def test_stable_draft_requires_a_source():
    try:
        RecordDraft(record_status="stable")
        raise AssertionError("stable records without provenance must be rejected")
    except ValueError as exc:
        assert "stability_source" in str(exc)


def test_neo4j_create_receives_stability_fields():
    class CapturingSession:
        def __init__(self):
            self.parameters = None
            self.cypher = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, cypher, **parameters):
            self.cypher = cypher
            self.parameters = parameters

    class CapturingDriver:
        def __init__(self):
            self.current_session = CapturingSession()

        def session(self):
            return self.current_session

    store = object.__new__(SkinRecordStore)
    store.driver = CapturingDriver()
    store.create(
        "u1",
        {
            "date": date.today(),
            "issue": "acne",
            "severity": 2,
            "zone": "chin",
            "record_status": "stable",
            "stability_source": "agent_confirmed",
            "products": [],
        },
    )

    session = store.driver.current_session
    assert "record_status: $record_status" in session.cypher
    assert "stability_source: $stability_source" in session.cypher
    assert session.parameters["record_status"] == "stable"
    assert session.parameters["stability_source"] == "agent_confirmed"


if __name__ == "__main__":
    test_explicit_stability_is_user_reported_without_extra_question()
    test_ambiguous_stability_requires_a_separate_confirmation()
    test_unconfirmed_ambiguous_stability_stays_recorded()
    test_general_record_confirmation_does_not_imply_stability()
    test_old_records_are_exposed_as_recorded()
    test_not_stable_signal_clears_previous_stability()
    test_stable_draft_requires_a_source()
    test_neo4j_create_receives_stability_fields()
    print("Skin Tracking stability tests passed")


def test_current_report_does_not_call_llm_by_default(monkeypatch):
    store = InMemoryStore()
    store.create(
        "u1",
        {
            "date": date.today(),
            "issue": "acne",
            "severity": 2,
            "zone": "chin",
            "products": [],
        },
    )

    class ExplodingLlm:
        def chat(self, _messages):
            raise AssertionError("report GET must not synchronously call an LLM")

    monkeypatch.delenv("SKIN_REPORT_SYNC_LLM", raising=False)
    result = SkinRecordService(store, llm=ExplodingLlm()).current_report("u1")
    assert result["records"]
    assert result["report"] is not None
