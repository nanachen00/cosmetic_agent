"""Feature 2 tests for the four published Skin Tracking zones."""
from __future__ import annotations

import os
import sys

from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.skin_tracking.extraction import (
    EXTRACTION_INSTRUCTIONS,
    ZONE_EXTRACTION_RULES,
    SkinExtractionResult,
)
from schemas.tracking_schema import RecordDraft, Zone
from tools.agent.tracking.skin_extract_tool import merge_extracted_drafts


def test_zone_taxonomy_matches_the_published_face_map():
    assert {zone.value for zone in Zone} == {
        "t_zone",
        "left_cheek",
        "right_cheek",
        "chin",
    }


def test_zone_rules_are_part_of_every_terra_request():
    rendered = EXTRACTION_INSTRUCTIONS.format(today="2026-08-17")
    assert ZONE_EXTRACTION_RULES in rendered


def test_zone_rules_cover_boundaries_and_safe_ambiguity():
    for phrase in (
        "使用者本人",
        "眉心",
        "鼻翼",
        "左眼周",
        "右眼周",
        "人中",
        "嘴角",
        "兩頰",
        "zone 保持 null",
        "太陽穴",
        "下顎線",
    ):
        assert phrase in ZONE_EXTRACTION_RULES


def test_bilateral_cheeks_remain_two_drafts():
    extracted = SkinExtractionResult.model_validate(
        {
            "records": [
                {"issue": "dryness", "zone": "left_cheek"},
                {"issue": "dryness", "zone": "right_cheek"},
            ]
        }
    )

    drafts = merge_extracted_drafts([], extracted.records)

    assert [draft["zone"] for draft in drafts] == ["left_cheek", "right_cheek"]
    assert [draft["issue"] for draft in drafts] == ["dryness", "dryness"]


def test_ambiguous_zone_remains_a_missing_required_slot():
    draft = RecordDraft(issue="dryness", severity=2, duration_days=1)
    assert "zone" in draft.missing_slots()


def test_unpublished_zone_is_rejected_by_the_schema():
    try:
        SkinExtractionResult.model_validate(
            {"records": [{"issue": "dryness", "zone": "whole_face"}]}
        )
        raise AssertionError("unpublished zones must not enter the domain model")
    except ValidationError:
        pass


if __name__ == "__main__":
    test_zone_taxonomy_matches_the_published_face_map()
    test_zone_rules_are_part_of_every_terra_request()
    test_zone_rules_cover_boundaries_and_safe_ambiguity()
    test_bilateral_cheeks_remain_two_drafts()
    test_ambiguous_zone_remains_a_missing_required_slot()
    test_unpublished_zone_is_rejected_by_the_schema()
    print("Skin Tracking zone extraction tests passed")
