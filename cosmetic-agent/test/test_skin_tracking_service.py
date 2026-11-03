"""Skin Tracking facade/session tests; no Root, HTTP app, Neo4j or API required."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.skin_tracking.extraction import SkinConcernAssessment, SkinExtractionResult
from agents.skin_tracking.extraction import SeverityInterpretation
from agents.skin_tracking.service import SkinRecordService, SkinTrackingService
from agents.skin_tracking.session_store import SkinSessionStore
from agents.skin_tracking.tracking_agent import (
    SkinTrackingAgent,
    configure,
    reset_configuration,
    run,
)
from tools.agent.tracking.skin_record_tool import longest_recent_streak
from tools.agent.tracking.skin_extract_tool import extract_skin_drafts_from_message


class StubExtractor:
    def extract(self, message, *, today):
        record = {}
        if "開始記錄膚況" in message:
            record = {"intent": "create"}
        elif "下巴" in message:
            record = {"issue": "acne", "zone": "chin"}
        elif "3分" in message:
            record = {"severity": 3, "duration_days": 1}
        return SkinExtractionResult.model_validate({"records": [record]})


class FakeStore:
    def __init__(self):
        self.records = {}

    def create(self, user_id, record):
        record_id = str(uuid.uuid4())
        self.records[record_id] = {"record_id": record_id, "user_id": user_id, **record}
        return record_id

    def query(self, user_id, *_args, **_kwargs):
        return [r for r in self.records.values() if r["user_id"] == user_id]

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

    def check_eligibility(self, user_id, today=None, min_days=5):
        today = today or date.today()
        dates = [r["date"] for r in self.records.values() if r["user_id"] == user_id]
        streak, streak_dates = longest_recent_streak(dates, today)
        return {
            "eligible": streak >= min_days,
            "consecutive_days": streak,
            "days_needed": max(0, min_days - streak),
            "streak_dates": [str(d) for d in streak_dates],
        }


def test_session_continues_until_confirmed():
    store = FakeStore()
    service = SkinTrackingService(
        SkinTrackingAgent(StubExtractor(), store),
        SkinSessionStore(),
    )

    first = service.send("u1", "s1", "cycle_skin_tracking")
    assert first["stage"] == "skin_tracking_collect"

    second = service.send("u1", "s1", "下巴長痘痘")
    assert "我確認一下" in second["reply"]
    assert [item["value"] for item in second["quick_replies"]] == [
        "1", "2", "3", "4", "5",
    ]

    confirm_card = service.send("u1", "s1", "3分，持續一天")
    assert confirm_card["stage"] == "skin_tracking_confirm"
    assert "痘痘" in confirm_card["reply"]
    assert not confirm_card["cards"]
    assert not store.records

    completed = service.send("u1", "s1", "確認")
    assert completed["done"] is True
    assert len(store.records) == 1
    assert service.get_session("u1", "s1") is None


def test_single_followup_keeps_all_multi_zone_drafts():
    class OneAnswerExtractor:
        def extract(self, _message, *, today):
            return SkinExtractionResult.model_validate(
                {"records": [{"severity": 4, "duration_days": 2}]}
            )

    drafts = [
        {"intent": "create", "issue": "acne", "zone": "chin"},
        {"intent": "create", "issue": "oiliness", "zone": "t_zone"},
    ]
    merged = extract_skin_drafts_from_message(OneAnswerExtractor(), drafts, "第一筆 4 分兩天")
    assert len(merged) == 2
    assert merged[0]["severity"] == 4
    assert merged[1]["issue"] == "oiliness"


def test_other_quick_reply_requests_a_real_description_before_zone():
    class OtherIssueExtractor:
        def extract(self, message, *, today):
            if "開始記錄膚況" in message:
                record = {"intent": "create"}
            elif "刺刺" in message:
                record = {"issue": "other", "note": message}
            else:
                record = {}
            return SkinExtractionResult.model_validate({"records": [record]})

    service = SkinTrackingService(
        SkinTrackingAgent(OtherIssueExtractor(), FakeStore()),
        SkinSessionStore(),
    )

    service.send("u1", "other-issue", "cycle_skin_tracking")
    description_question = service.send("u1", "other-issue", "其他")
    assert description_question["stage"] == "skin_tracking_describe_issue"
    assert "形容" in description_question["reply"]
    assert "部位" not in description_question["reply"]

    zone_question = service.send("u1", "other-issue", "洗完臉會刺刺的")
    assert zone_question["stage"] == "skin_tracking_collect"
    assert "哪個部位" in zone_question["reply"]
    assert "洗完臉會刺刺的" in zone_question["reply"]
    assert "不是確診" in zone_question["reply"]
    assert "是其他" not in zone_question["reply"]


def test_unspecified_eye_area_never_invents_a_left_or_right_side():
    class GuessingExtractor:
        def extract(self, message, *, today):
            return SkinExtractionResult.model_validate({
                "records": [{
                    "intent": "create",
                    "issue": "other",
                    "zone": "left_cheek",
                    "note": message,
                }]
            })

    drafts = extract_skin_drafts_from_message(
        GuessingExtractor(),
        [],
        "眼瞼下面有很多小顆粒",
    )
    assert drafts[0]["zone"] is None

    explicit = extract_skin_drafts_from_message(
        GuessingExtractor(),
        [],
        "左眼下面有很多小顆粒",
    )
    assert explicit[0]["zone"] == "left_cheek"


def test_model_reasons_about_the_specific_concern_before_severity():
    class ReasoningExtractor:
        def __init__(self):
            self.assessment_calls = []

        def extract(self, message, *, today):
            if "開始記錄膚況" in message:
                record = {"intent": "create"}
            elif "脂肪粒" in message:
                record = {"issue": "other", "note": message}
            else:
                record = {}
            return SkinExtractionResult.model_validate({"records": [record]})

        def assess_concern(self, **kwargs):
            self.assessment_calls.append(kwargs)
            return SkinConcernAssessment.model_validate({
                "concern_summary": "眼下出現像脂肪粒的小顆粒",
                "clarifying_question": "這些小顆粒是在左眼下、右眼下，還是兩邊都有？",
                "severity_levels": [
                    {"level": 1, "description": "摸起來才感覺得到"},
                    {"level": 2, "description": "近看有少量小顆粒"},
                    {"level": 3, "description": "一眼可見或數量變多"},
                    {"level": 4, "description": "範圍擴大並伴隨不適"},
                    {"level": 5, "description": "快速變多、紅腫或疼痛"},
                ],
            })

    extractor = ReasoningExtractor()
    service = SkinTrackingService(
        SkinTrackingAgent(extractor, FakeStore()),
        SkinSessionStore(),
    )

    service.send("u1", "reasoned-severity", "cycle_skin_tracking")
    zone_question = service.send(
        "u1", "reasoned-severity", "眼睛下面長一粒粒很像脂肪粒的東西"
    )
    assert "眼下出現像脂肪粒的小顆粒" in zone_question["reply"]
    assert "左眼下、右眼下，還是兩邊" in zone_question["reply"]
    assert "不是確診" in zone_question["reply"]

    severity_question = service.send(
        "u1", "reasoned-severity", "左臉頰區（含左眼周）"
    )
    assert "眼下出現像脂肪粒的小顆粒" in severity_question["reply"]
    assert "對嗎" in severity_question["reply"]
    assert [item["value"] for item in severity_question["quick_replies"]] == [
        "1", "2", "3", "4", "5",
    ]
    assert extractor.assessment_calls[0]["user_description"] == (
        "眼睛下面長一粒粒很像脂肪粒的東西"
    )


def test_fixed_zone_buttons_expand_multi_select_without_model_guessing():
    class ZoneButtonExtractor:
        def __init__(self):
            self.messages = []

        def extract(self, message, *, today):
            self.messages.append(message)
            record = (
                {"intent": "create", "issue": "acne"}
                if message == "痘痘"
                else {}
            )
            return SkinExtractionResult.model_validate({"records": [record]})

    extractor = ZoneButtonExtractor()
    service = SkinTrackingService(
        SkinTrackingAgent(extractor, FakeStore()),
        SkinSessionStore(),
    )

    service.send("u1", "zone-buttons", "cycle_skin_tracking")
    zone_question = service.send("u1", "zone-buttons", "痘痘")
    assert "部位" in zone_question["reply"]

    selected = service.send(
        "u1",
        "zone-buttons",
        "T字部位（額頭、鼻子）、左臉頰區（含左眼周）",
    )
    entry = service.sessions.get("zone-buttons", "u1")
    assert entry is not None
    assert [
        draft["zone"]
        for draft in entry.state["drafts"]
    ] == ["t_zone", "left_cheek"]
    assert all(
        "T字部位" not in message
        for message in extractor.messages
    )


def test_explicit_configuration():
    reset_configuration()
    try:
        run("開始記錄膚況", user_id="u1")
        raise AssertionError("unconfigured run must fail")
    except RuntimeError as exc:
        assert "configure" in str(exc)

    configure(StubExtractor(), FakeStore())
    result = run("開始記錄膚況", user_id="u1")
    assert result["stage"] == "skin_tracking_collect"
    reset_configuration()


def test_current_report_is_available_before_five_consecutive_days():
    store = FakeStore()
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

    result = SkinRecordService(store).current_report("u1")

    assert result["eligibility"]["eligible"] is True
    assert result["eligibility"]["days_needed"] == 0
    assert result["report"] is not None
    assert len(result["records"]) == 1


def test_chat_report_intent_links_to_visual_report_without_generating_long_text():
    class ReportIntentExtractor:
        def extract(self, _message, *, today):
            return SkinExtractionResult.model_validate(
                {"records": [{"intent": "report"}]}
            )

    class ReportModelMustNotRun:
        def chat(self, *_args, **_kwargs):
            raise AssertionError("chat report must not generate a text report")

    store = FakeStore()
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
    result = SkinTrackingAgent(
        ReportIntentExtractor(),
        store,
        report_model=ReportModelMustNotRun(),
    ).handle("我要看膚況分析表", "u1")

    assert result["done"] is True
    assert "report" not in result
    assert len(result["reply"]) < 80
    assert result["quick_replies"] == [
        {"label": "前往膚況分析報告", "value": "open_skin_report"}
    ]


def test_bilateral_same_concern_asks_severity_once_and_deduplicates_completion(monkeypatch):
    class BilateralExtractor:
        def extract(self, message, *, today):
            if "雙臉" in message:
                records = [
                    {
                        "intent": "create",
                        "issue": "redness",
                        "zone": "left_cheek",
                        "note": message,
                    },
                    {
                        "intent": "create",
                        "issue": "redness",
                        "zone": "right_cheek",
                        "note": message,
                    },
                ]
            else:
                records = [{}]
            return SkinExtractionResult.model_validate({"records": records})

        def assess_concern(self, **kwargs):
            assert kwargs["zone_label"] == "雙側臉頰區（含左右眼周）"
            return SkinConcernAssessment.model_validate({
                "concern_summary": "使用者自述脂漏性皮膚炎相關的雙側泛紅",
                "clarifying_question": "目前有沒有伴隨脫屑、刺癢或灼熱感？",
                "severity_levels": [
                    {"level": 1, "description": "少量淡紅，近看才明顯"},
                    {"level": 2, "description": "局部淡紅，平時不太明顯"},
                    {"level": 3, "description": "雙側有明顯泛紅區域"},
                    {"level": 4, "description": "大片持續泛紅，遠看也明顯"},
                    {"level": 5, "description": "廣泛深紅並伴隨明顯不適"},
                ],
            })

    monkeypatch.setenv("BLINK_TESTING_UNLOCK_ALL", "true")
    store = FakeStore()
    agent = SkinTrackingAgent(BilateralExtractor(), store)

    severity_question = agent.handle("我的泛紅是雙臉脂漏性皮膚炎", "u1")
    assert "雙側" in severity_question["reply"]
    assert "只要選一次" in severity_question["reply"]
    assert "再看另一個部位" not in severity_question["reply"]

    confirmation = agent.handle("4", "u1", severity_question["session"])
    assert confirmation["stage"] == "skin_tracking_confirm"
    assert "雙側臉頰區" in confirmation["reply"]
    assert "第 2 筆" not in confirmation["reply"]

    completed = agent.handle("確認", "u1", confirmation["session"])
    assert completed["done"] is True
    assert len(store.records) == 2
    assert completed["reply"].count("今天的膚況我幫你收好了") == 1
    assert completed["quick_replies"] == [
        {"label": "前往膚況分析報告", "value": "open_skin_report"}
    ]


if __name__ == "__main__":
    test_session_continues_until_confirmed()
    test_single_followup_keeps_all_multi_zone_drafts()
    test_explicit_configuration()
    test_current_report_stays_locked_before_five_consecutive_days()
    print("SkinTrackingService tests passed")

def _severity_assessment(summary):
    return SkinConcernAssessment.model_validate({
        "concern_summary": summary,
        "clarifying_question": "這個狀況目前大約影響多大範圍？",
        "severity_levels": [
            {"level": 1, "description": "只有少量，近看才看得出來"},
            {"level": 2, "description": "局部可見，但整體不太明顯"},
            {"level": 3, "description": "數小時後明顯，需要簡單處理"},
            {"level": 4, "description": "大範圍持續，已影響舒適感"},
            {"level": 5, "description": "非常明顯且反覆，影響日常"},
        ],
    })


class FreeTextSeverityExtractor:
    def __init__(self, decision, records=None, raises=False):
        self.decision = decision
        self.records = records or [
            {"intent": "create", "issue": "oiliness", "zone": "t_zone", "note": "容易出油"}
        ]
        self.raises = raises
        self.extracted_messages = []
        self.interpretation_calls = []

    def extract(self, message, *, today):
        self.extracted_messages.append(message)
        if "開始記錄膚況" in message:
            records = [{"intent": "create"}]
        elif "出油" in message or "兩側眼周乾燥" in message:
            records = self.records
        else:
            records = [{}]
        return SkinExtractionResult.model_validate({"records": records})

    def assess_concern(self, **kwargs):
        return _severity_assessment(kwargs["issue_label"])

    def interpret_severity(self, **kwargs):
        self.interpretation_calls.append(kwargs)
        if self.raises:
            raise RuntimeError("temporary model outage")
        return self.decision


def _free_text_service(extractor):
    return SkinTrackingService(
        SkinTrackingAgent(extractor, FakeStore()),
        SkinSessionStore(),
    )


def test_free_text_severity_is_saved_without_reextracting():
    extractor = FreeTextSeverityExtractor(SeverityInterpretation.model_validate({
        "severity": 3,
        "understood_description": "T字部位早上出油不明顯，但下午油光較明顯",
        "needs_clarification": False,
        "clarifying_question": None,
    }))
    service = _free_text_service(extractor)
    service.send("u-free", "s-free", "cycle_skin_tracking")
    question = service.send("u-free", "s-free", "T字部位容易出油")
    assert [item["value"] for item in question["quick_replies"]] == [
        "1", "2", "3", "4", "5",
    ]
    extracted_before = list(extractor.extracted_messages)

    result = service.send("u-free", "s-free", "下午時特別容易出油，但早上還好")

    assert result["stage"] == "skin_tracking_confirm"
    assert "程度中度" in result["reply"]
    assert "下午時特別容易出油，但早上還好" in result["reply"]
    assert "較接近選項" not in result["reply"]
    session = service.sessions.get("s-free", "u-free").state
    assert session["drafts"][0]["severity"] == 3
    assert "下午時特別容易出油，但早上還好" in session["drafts"][0]["note"]
    assert extractor.extracted_messages == extracted_before


def test_vague_free_text_keeps_severity_question_and_draft():
    extractor = FreeTextSeverityExtractor(SeverityInterpretation.model_validate({
        "severity": None,
        "understood_description": None,
        "needs_clarification": True,
        "clarifying_question": "油光大約多久後會變明顯？",
    }))
    service = _free_text_service(extractor)
    service.send("u-vague", "s-vague", "cycle_skin_tracking")
    service.send("u-vague", "s-vague", "T字部位容易出油")
    extracted_before = list(extractor.extracted_messages)

    result = service.send("u-vague", "s-vague", "有一點油")

    assert result["stage"] == "skin_tracking_collect"
    assert result["reply"] == "油光大約多久後會變明顯？"
    assert [item["value"] for item in result["quick_replies"]] == [
        "1", "2", "3", "4", "5",
    ]
    assert service.sessions.get("s-vague", "u-vague").state["drafts"][0]["severity"] is None
    assert extractor.extracted_messages == extracted_before


def test_severity_interpreter_outage_does_not_reset_flow():
    extractor = FreeTextSeverityExtractor(None, raises=True)
    service = _free_text_service(extractor)
    service.send("u-outage", "s-outage", "cycle_skin_tracking")
    service.send("u-outage", "s-outage", "T字部位容易出油")
    extracted_before = list(extractor.extracted_messages)

    result = service.send("u-outage", "s-outage", "下午比較油")

    assert result["stage"] == "skin_tracking_collect"
    assert [item["value"] for item in result["quick_replies"]] == [
        "1", "2", "3", "4", "5",
    ]
    assert service.sessions.get("s-outage", "u-outage").state["drafts"][0]["severity"] is None
    assert extractor.extracted_messages == extracted_before


def test_free_text_severity_is_shared_by_bilateral_peer_once(monkeypatch):
    records = [
        {"intent": "create", "issue": "dryness", "zone": "left_cheek", "note": "兩側眼周乾燥"},
        {"intent": "create", "issue": "dryness", "zone": "right_cheek", "note": "兩側眼周乾燥"},
    ]
    extractor = FreeTextSeverityExtractor(SeverityInterpretation.model_validate({
        "severity": 2,
        "understood_description": "兩側眼周只有局部乾燥",
        "needs_clarification": False,
        "clarifying_question": None,
    }), records=records)
    monkeypatch.setenv("BLINK_TESTING_UNLOCK_ALL", "true")
    agent = SkinTrackingAgent(extractor, FakeStore())
    question = agent.handle("兩側眼周乾燥", "u-both")
    # Simulate the existing location-selection step completing both eye areas.
    question["session"]["drafts"][0]["zone"] = "left_cheek"
    question["session"]["drafts"][1]["zone"] = "right_cheek"
    result = agent.handle(
        "只有眼下小範圍有點乾", "u-both", question["session"]
    )

    assert result["stage"] == "skin_tracking_confirm"
    session = result["session"]
    assert [draft["severity"] for draft in session["drafts"]] == [2, 2]
    assert all("只有眼下小範圍有點乾" in draft["note"] for draft in session["drafts"])
    assert len(extractor.interpretation_calls) == 1

class ButtonOnlyZoneExtractor:
    def __init__(self):
        self.extracted_messages = []

    def extract(self, message, *, today):
        self.extracted_messages.append(message)
        if "開始記錄膚況" in message:
            record = {"intent": "create"}
        elif "黑眼圈" in message:
            record = {"intent": "create", "issue": "other", "note": message}
        elif "其實是乾燥" in message:
            record = {"issue": "dryness", "note": message}
        else:
            record = {}
        return SkinExtractionResult.model_validate({"records": [record]})

    def assess_concern(self, **kwargs):
        return _severity_assessment("明顯黑眼圈")


def _button_only_zone_service():
    extractor = ButtonOnlyZoneExtractor()
    service = SkinTrackingService(
        SkinTrackingAgent(extractor, FakeStore()),
        SkinSessionStore(),
    )
    service.send("u-zone", "s-zone", "cycle_skin_tracking")
    service.send("u-zone", "s-zone", "最近黑眼圈很重")
    return service, extractor


def test_free_text_eye_area_is_rejected_and_waits_for_fixed_button():
    service, extractor = _button_only_zone_service()
    extracted_before = list(extractor.extracted_messages)

    result = service.send("u-zone", "s-zone", "眼周")

    assert result["stage"] == "skin_tracking_collect"
    assert result["reply"] == "部位請直接使用下方固定按鈕選擇，可以複選。"
    assert [item["label"] for item in result["quick_replies"]] == [
        "T字部位（額頭、鼻子）",
        "左臉頰區（含左眼周）",
        "右臉頰區（含右眼周）",
        "下巴區（嘴周、人中、下巴）",
    ]
    session = service.sessions.get("s-zone", "u-zone").state
    assert session["drafts"][0]["zone"] is None
    assert extractor.extracted_messages == extracted_before


def test_unknown_location_never_creates_a_custom_zone():
    service, extractor = _button_only_zone_service()
    extracted_before = list(extractor.extracted_messages)

    result = service.send("u-zone", "s-zone", "脖子附近")

    assert result["stage"] == "skin_tracking_collect"
    assert len(result["quick_replies"]) == 4
    assert service.sessions.get("s-zone", "u-zone").state["drafts"][0]["zone"] is None
    assert extractor.extracted_messages == extracted_before


def test_button_only_zone_step_keeps_explicit_concern_correction():
    service, extractor = _button_only_zone_service()

    result = service.send("u-zone", "s-zone", "其實是乾燥")

    assert extractor.extracted_messages[-1] == "其實是乾燥"
    session = service.sessions.get("s-zone", "u-zone").state
    assert session["drafts"][0]["issue"] == "dryness"
    assert session["drafts"][0]["zone"] is None
    assert "固定部位按鈕" in result["reply"]


def test_known_concern_with_missing_zone_skips_redundant_assessment():
    class KnownConcernExtractor:
        def __init__(self):
            self.assessment_calls = 0

        def extract(self, message, *, today):
            return SkinExtractionResult.model_validate({
                "records": [{
                    "intent": "create",
                    "issue": "dryness",
                    "severity": 2,
                    "zone": None,
                    "detail_zones": ["cheek"],
                    "note": message,
                }]
            })

        def assess_concern(self, **kwargs):
            self.assessment_calls += 1
            return _severity_assessment("不應呼叫")

    extractor = KnownConcernExtractor()
    result = SkinTrackingAgent(extractor, FakeStore()).handle(
        "今天臉頰乾燥、輕微泛紅", "u-known-zone"
    )

    assert result["stage"] == "skin_tracking_collect"
    assert "固定部位按鈕" in result["reply"]
    assert extractor.assessment_calls == 0
