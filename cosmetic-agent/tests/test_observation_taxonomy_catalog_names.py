from agents.skin_tracking.observation_taxonomy import (
    extract_observation_metadata,
    load_catalog,
)


def test_catalog_has_at_least_100_formal_observation_labels():
    assert len(load_catalog()) >= 100


def test_exact_formal_condition_name_is_preserved_as_tag():
    catalog = load_catalog()
    condition = next(
        tag for tag in catalog if tag.slug == "seborrheic_dermatitis_pattern"
    )
    assert condition.name_zh_tw == "脂漏性皮膚炎樣表現"

    tags, _ = extract_observation_metadata(f"鼻翼有{condition.name_zh_tw}")

    assert condition.slug in tags


def test_localized_pruritus_is_a_formal_non_diagnostic_observation():
    tags, _ = extract_observation_metadata("眉心搔癢")
    itch = next(tag for tag in load_catalog() if tag.slug == "localized_pruritus")
    assert itch.slug in tags

