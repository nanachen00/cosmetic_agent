import copy
import json

from agents.skin_tracking import service as service_module
from agents.skin_tracking.service import SkinRecordService
from tools.agent.tracking.report_note_display import build_report_display_notes


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _record(record_id, zone, note, severity=2):
    return {
        "record_id": record_id,
        "date": "2026-09-26",
        "zone": zone,
        "issue": "acne_scar",
        "severity": severity,
        "note": note,
        "products": [],
    }


def _reply(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def _example_records():
    return [
        _record("left-001", "left_cheek", "左邊有淡淡痘印"),
        _record("right-001", "right_cheek", "右邊一個禮拜了還是很明顯", 5),
    ]


def _example_reply():
    return _reply(
        {"record_id": "left-001", "display_note": "有淡淡痘印"},
        {"record_id": "right-001", "display_note": "痘疤已持續一個禮拜，仍然明顯"},
    )


def test_model_receives_separate_regions_and_returns_display_only_notes():
    records = _example_records()
    original = copy.deepcopy(records)
    llm = FakeLLM(_example_reply())

    result = build_report_display_notes(llm, records)

    assert result == {
        "left-001": "有淡淡痘印",
        "right-001": "痘疤已持續一個禮拜，仍然明顯",
    }
    assert records == original
    assert len(llm.calls) == 1
    prompt = llm.calls[0][0]["content"]
    sent = json.loads(prompt.split("資料 JSON：\n", 1)[1])["records"]
    assert [(item["record_id"], item["zone"]) for item in sent] == [
        ("left-001", "left_cheek"), ("right-001", "right_cheek")
    ]
    assert all("severity" not in item for item in sent)


def test_combined_original_note_is_split_before_model_call():
    combined = "左邊有淡淡痘印，右邊一個禮拜了還是很明顯"
    records = [
        _record("left-001", "left_cheek", combined),
        _record("right-001", "right_cheek", combined, 5),
    ]
    llm = FakeLLM(_example_reply())

    result = build_report_display_notes(llm, records)

    sent = json.loads(llm.calls[0][0]["content"].split("資料 JSON：\n", 1)[1])["records"]
    assert sent[0]["note"] == "左邊有淡淡痘印"
    assert sent[1]["note"] == "右邊一個禮拜了還是很明顯"
    assert result["left-001"] == "有淡淡痘印"
    assert result["right-001"].startswith("痘疤已持續")


def test_wrong_side_model_text_is_rejected():
    llm = FakeLLM(_reply({"record_id": "left-001", "display_note": "右邊有淡淡痘印"}))
    assert build_report_display_notes(llm, [_example_records()[0]]) == {}


def test_lost_duration_is_rejected():
    llm = FakeLLM(_reply({"record_id": "right-001", "display_note": "痘疤仍然明顯"}))
    assert build_report_display_notes(llm, [_example_records()[1]]) == {}


def test_model_error_and_empty_note_do_not_block_report():
    assert build_report_display_notes(FakeLLM(RuntimeError("offline")), _example_records()) == {}
    llm = FakeLLM("should not be called")
    assert build_report_display_notes(llm, [_record("none", "left_cheek", "")]) == {}
    assert llm.calls == []


def test_unlocked_report_includes_display_note_without_changing_stored_note():
    records = _example_records()
    original = copy.deepcopy(records)

    class Store:
        def check_eligibility(self, user_id, today, min_days):
            return {"eligible": False, "consecutive_days": 1, "days_needed": 4, "streak_dates": []}

        def query(self, user_id, date_from, date_to):
            return records

    result = SkinRecordService(Store(), FakeLLM(_example_reply())).current_report("user-1")

    assert result["eligibility"]["eligible"] is True
    assert result["eligibility"]["days_needed"] == 0
    assert result["report"] is not None
    assert result["records"][0]["display_note"] == "有淡淡痘印"
    assert result["records"][1]["display_note"] == "痘疤已持續一個禮拜，仍然明顯"
    assert [item["note"] for item in result["records"]] == [item["note"] for item in original]
    assert records == original
