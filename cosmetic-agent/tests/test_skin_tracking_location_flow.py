from agents.skin_tracking.extraction import ExtractedSkinRecord, SkinExtractionResult
from agents.skin_tracking.tracking_agent import SkinTrackingAgent


class RednessExtractor:
    def extract(self, message, *, today):
        return SkinExtractionResult(
            records=[
                ExtractedSkinRecord(intent="create", issue="redness")
            ]
        )


def _agent(extractor=None) -> SkinTrackingAgent:
    # These sentinels intentionally have no extraction or persistence methods.
    # Location-only branches must finish before either dependency is touched.
    return SkinTrackingAgent(extractor=extractor or object(), store=object())


def _zone_session() -> dict:
    return {
        "phase": "collect",
        "draft": {"issue": "dryness"},
    }


def _drafts(result: dict) -> list[dict]:
    session = result["session"]
    return session["drafts"] or [session["draft"]]


def test_structured_location_button_bypasses_semantic_extractor():
    result = _agent().handle(
        "left_cheek、right_cheek",
        user_id="u1",
        session=_zone_session(),
        action="record_location",
    )

    assert [draft["zone"] for draft in _drafts(result)] == [
        "left_cheek",
        "right_cheek",
    ]


def test_generic_eye_area_asks_laterality_then_keeps_precise_tag():
    first = _agent().handle("眼睛周圍", user_id="u1", session=_zone_session())

    assert "左邊、右邊，還是兩邊" in first["reply"]
    assert first["session"]["pending_location"] is not None

    second = _agent().handle("左邊", user_id="u1", session=first["session"])
    draft = _drafts(second)[0]
    assert draft["zone"] == "left_cheek"
    assert "periorbital" in draft["detail_zones"]
    assert second["session"]["pending_location"] is None


def test_off_prompt_answer_does_not_invent_a_location():
    result = _agent(RednessExtractor()).handle(
        "最近有點泛紅",
        user_id="u1",
        session=_zone_session(),
    )

    draft = _drafts(result)[0]


def test_structured_location_button_stays_direct_without_existing_session():
    result = _agent().handle(
        "chin",
        user_id="u1",
        session=None,
        action="record_location",
    )

    assert _drafts(result)[0]["zone"] == "chin"


def test_structured_issue_button_bypasses_semantic_extractor_and_uses_catalog():
    result = _agent().handle(
        "乾燥",
        user_id="u1",
        session=None,
        action="record_issue",
    )

    draft = _drafts(result)[0]
    assert draft["issue"] == "dryness"
    assert "skin_dryness" in draft["tag_slugs"]
    assert "哪個部位" in result["reply"]
    assert result["quick_replies"]


def test_other_issue_button_resets_stale_draft_without_extractor():
    result = _agent().handle(
        "其他狀況…",
        user_id="u1",
        session={
            "phase": "collect",
            "draft": {"issue": "dryness", "zone": "forehead_nose"},
        },
        action="record_other_issue",
    )

    assert result["session"]["phase"] == "describe_issue"
    assert result["session"]["draft"] == {}
    assert result["session"]["drafts"] == []
    assert "直接跟我形容" in result["reply"]


def test_free_text_adds_precise_location_and_formal_observation_metadata():
    result = _agent(RednessExtractor()).handle(
        "眉心一直泛紅",
        user_id="u1",
        session=None,
    )

    draft = _drafts(result)[0]
    assert draft["zone"] == "t_zone"
    assert "glabella" in draft["detail_zones"]
    assert "diffuse_facial_redness" in draft["tag_slugs"]
    assert "眉心" in result["reply"]
