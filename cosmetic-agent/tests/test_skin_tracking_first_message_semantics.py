from agents.skin_tracking.extraction import ExtractedSkinRecord, SkinExtractionResult
from agents.skin_tracking.tracking_agent import SkinTrackingAgent


class EmptyExtractor:
    def extract(self, message, *, today):
        return SkinExtractionResult(records=[ExtractedSkinRecord(intent="create")])


def _agent() -> SkinTrackingAgent:
    return SkinTrackingAgent(extractor=EmptyExtractor(), store=object())


def _drafts(result: dict) -> list[dict]:
    session = result["session"]
    return session["drafts"] or [session["draft"]]


def test_complete_first_reply_does_not_restart_opening_prompt():
    agent = _agent()
    opening = agent.handle("cycle_skin_tracking", user_id="u1", session=None)
    result = agent.handle(
        "鼻翼兩側一直脫皮",
        user_id="u1",
        session=opening["session"],
    )

    draft = _drafts(result)[0]
    assert draft["issue"] == "dryness"
    assert draft["zone"] == "t_zone"
    assert draft["detail_zones"] == ["nasal_ala"]
    assert "最近有哪個變化" not in result["reply"]
    assert [reply.get("value") for reply in result["quick_replies"]] == ["1", "2", "3", "4", "5"]


def test_partial_first_reply_keeps_issue_and_asks_for_location():
    result = _agent().handle(
        "最近開始脫皮",
        user_id="u1",
        session=None,
    )

    draft = _drafts(result)[0]
    assert draft["issue"] == "dryness"
    assert draft["zone"] is None
    assert "哪個部位" in result["reply"]
    assert all(reply.get("action") == "record_location" for reply in result["quick_replies"])


def test_unrelated_first_reply_does_not_invent_issue_or_location():
    result = _agent().handle(
        "最近工作壓力很大",
        user_id="u1",
        session=None,
    )

    assert _drafts(result)[0]["issue"] is None
    assert _drafts(result)[0]["zone"] is None
    assert "最近有哪個變化" in result["reply"]


def test_bilateral_detailed_location_creates_two_macro_zone_drafts():
    result = _agent().handle(
        "兩邊上眼皮都有點紅",
        user_id="u1",
        session=None,
    )

    drafts = _drafts(result)
    assert [draft["issue"] for draft in drafts] == ["redness", "redness"]
    assert [draft["zone"] for draft in drafts] == ["left_cheek", "right_cheek"]
    assert all(draft["detail_zones"] == ["upper_eyelid"] for draft in drafts)


def test_negated_issue_is_not_recorded():
    result = _agent().handle(
        "沒有痘痘，只有鼻翼脫皮",
        user_id="u1",
        session=None,
    )

    drafts = _drafts(result)
    assert [draft["issue"] for draft in drafts] == ["dryness"]
    assert drafts[0]["detail_zones"] == ["nasal_ala"]
