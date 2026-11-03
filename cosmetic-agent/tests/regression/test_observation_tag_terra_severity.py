import pytest
from pydantic import ValidationError

from agents.skin_tracking.extraction import (
    ConcernSeverityLevel,
    ExtractedSkinRecord,
    SkinConcernAssessment,
    _promote_tag_only_record,
)
from agents.skin_tracking.observation_taxonomy import (
    catalog_by_slug,
    extract_observation_metadata,
)
from schemas.tracking_schema import Issue


@pytest.mark.parametrize(
    ("message", "expected_slug"),
    [
        ("最近黑眼圈很重", "periorbital_darkening"),
        ("最近眼袋很明顯", "under_eye_puffiness"),
        ("眼周出現細紋", "fine_lines"),
        ("嘴唇乾裂", "chapped_lips"),
        ("最近膚色不均", "uneven_skin_tone"),
    ],
)
def test_catalog_observation_becomes_trackable_issue(message, expected_slug):
    tag_slugs, detail_zones = extract_observation_metadata(message)
    record = ExtractedSkinRecord(
        tag_slugs=tag_slugs,
        detail_zones=detail_zones,
        note=message,
    )

    promoted = _promote_tag_only_record(record)

    assert expected_slug in promoted.tag_slugs
    assert promoted.issue is Issue.other
    assert promoted.note == message


def test_all_catalog_tags_can_satisfy_the_issue_slot():
    catalog = catalog_by_slug()

    assert len(catalog) >= 100
    for slug, tag in catalog.items():
        promoted = _promote_tag_only_record(ExtractedSkinRecord(tag_slugs=[slug]))

        assert promoted.issue is Issue.other
        assert promoted.tag_slugs == [slug]
        assert promoted.note == tag.name_zh_tw


def test_existing_core_issue_is_not_changed():
    record = ExtractedSkinRecord(
        issue=Issue.dryness,
        tag_slugs=["skin_dryness"],
        note="臉頰乾燥",
    )

    assert _promote_tag_only_record(record) == record


def test_unknown_text_does_not_become_other_issue():
    record = ExtractedSkinRecord(note="今天狀況和平常差不多")

    promoted = _promote_tag_only_record(record)

    assert promoted.issue is None
    assert promoted.tag_slugs is None


def test_empty_note_falls_back_to_catalog_label():
    record = ExtractedSkinRecord(tag_slugs=["periorbital_darkening"])

    promoted = _promote_tag_only_record(record)

    assert promoted.issue is Issue.other
    assert "黑眼圈" in promoted.note


def test_terra_severity_contract_requires_exactly_five_ordered_levels():
    levels = [
        ConcernSeverityLevel(level=level, description=f"程度 {level}")
        for level in range(1, 6)
    ]
    assessment = SkinConcernAssessment(
        concern_summary="兩側眼周黑眼圈",
        clarifying_question="請選擇最接近目前狀況的描述",
        severity_levels=levels,
    )

    assert [item.level for item in assessment.severity_levels] == [1, 2, 3, 4, 5]

    with pytest.raises(ValidationError):
        SkinConcernAssessment(
            concern_summary="兩側眼周黑眼圈",
            clarifying_question="請選擇最接近目前狀況的描述",
            severity_levels=levels[:4],
        )
