from schemas.tracking_schema import Zone

from agents.skin_tracking.location_semantics import (
    detail_location_labels,
    extract_face_locations,
    resolve_pending_location,
    zones_from_action,
)


def _summary(text: str) -> list[tuple[str, str, tuple[Zone, ...]]]:
    parsed = extract_face_locations(text)
    return [(item.region, item.laterality, item.macro_zones) for item in parsed.locations]


def test_left_lower_eyelid_maps_to_left_face_and_keeps_detail():
    parsed = extract_face_locations("今天左下眼皮很乾還有點脫皮")

    assert _summary("今天左下眼皮很乾還有點脫皮") == [
        ("lower_eyelid", "left", (Zone.left_cheek,))
    ]
    assert parsed.locations[0].detail_zones == ("lower_eyelid",)
    assert parsed.locations[0].label == "左下眼瞼"


def test_generic_eye_area_requests_laterality_then_resolves():
    parsed = extract_face_locations("眼睛周圍很乾")

    assert parsed.needs_laterality is True
    assert parsed.pending == {"locations": [{"region": "periorbital", "label": "眼周"}]}
    assert "左邊、右邊" in parsed.clarification

    resolved = resolve_pending_location(parsed.pending, "左邊")
    assert _summary("左眼周很乾") == [
        ("periorbital", "left", (Zone.left_cheek,))
    ]
    assert resolved.locations[0].label == "左眼周"
    assert resolved.locations[0].macro_zones == (Zone.left_cheek,)


def test_bilateral_cheeks_map_to_two_macro_zones():
    parsed = extract_face_locations("兩頰都很乾")

    assert parsed.needs_laterality is False
    assert parsed.locations[0].laterality == "bilateral"
    assert parsed.locations[0].macro_zones == (Zone.left_cheek, Zone.right_cheek)


def test_whole_face_maps_to_all_four_macro_zones():
    parsed = extract_face_locations("今天全臉都乾")

    assert parsed.locations[0].macro_zones == (
        Zone.t_zone,
        Zone.left_cheek,
        Zone.right_cheek,
        Zone.chin,
    )


def test_multiple_precise_regions_are_kept_in_order():
    parsed = extract_face_locations("額頭出油、兩頰很乾，鼻翼還起皮")

    assert [item.region for item in parsed.locations] == ["forehead", "cheek", "nasal_ala"]
    assert parsed.locations[1].macro_zones == (Zone.left_cheek, Zone.right_cheek)
    assert parsed.locations[2].detail_zones == ("nasal_ala",)


def test_different_sides_and_regions_do_not_bleed_into_each_other():
    parsed = extract_face_locations("右眼下黑眼圈，右嘴角又裂了")

    assert parsed.locations[0].region == "under_eye"
    assert parsed.locations[0].laterality == "right"
    assert parsed.locations[0].macro_zones == (Zone.right_cheek,)
    assert parsed.locations[1].region == "mouth_corner"
    assert parsed.locations[1].macro_zones == (Zone.chin,)


def test_negated_region_is_not_recorded():
    parsed = extract_face_locations("T字超油但臉頰沒事")

    assert [item.region for item in parsed.locations] == ["nose"]


def test_correction_prefers_non_negated_location():
    parsed = extract_face_locations("不是左臉，是右臉")

    assert len(parsed.locations) == 1
    assert parsed.locations[0].laterality == "right"
    assert parsed.locations[0].macro_zones == (Zone.right_cheek,)


def test_nose_tip_to_nasal_ala_correction():
    parsed = extract_face_locations("不是鼻頭，是鼻翼在脫皮")

    assert [item.region for item in parsed.locations] == ["nasal_ala"]


def test_generic_eyelid_keeps_detailed_display_tag():
    parsed = extract_face_locations("左眼皮很乾")

    assert parsed.locations[0].detail_zones == ("upper_eyelid", "lower_eyelid")
    assert detail_location_labels(Zone.left_cheek, parsed.locations[0].detail_zones) == ["左眼瞼"]


def test_philtrum_maps_to_chin_zone_but_keeps_precise_label():
    parsed = extract_face_locations("人中附近有點泛紅")

    assert parsed.locations[0].region == "philtrum"
    assert parsed.locations[0].label == "人中"
    assert parsed.locations[0].macro_zones == (Zone.chin,)


def test_structured_button_values_bypass_semantic_inference():
    assert zones_from_action("left_cheek、right_cheek") == [Zone.left_cheek, Zone.right_cheek]
    assert zones_from_action("T字部位（額頭、鼻子）") == [Zone.t_zone]
    assert zones_from_action("臉頰很乾") == []


def test_unrelated_or_partial_answer_does_not_invent_a_location():
    assert extract_face_locations("最近有點泛紅").recognized is False
    assert resolve_pending_location(None, "左邊").recognized is False
