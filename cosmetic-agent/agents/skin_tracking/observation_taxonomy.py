"""Controlled face-observation taxonomy and conservative text matching.

The catalog is presentation metadata, while SkinRecord stores only stable slugs and
detail-zone codes.  Disease-like tags are accepted from conversation text only
when the user explicitly names them; generic appearance words never become a
condition assertion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


CATALOG_PATH = Path(__file__).with_name("skin_observation_labels_100.json")


@dataclass(frozen=True)
class ObservationTag:
    slug: str
    name_zh_tw: str
    category: str
    face_regions: tuple[str, ...]
    condition_flag: bool
    source_urls: tuple[str, ...]

    @property
    def kind(self) -> str:
        return "condition" if self.condition_flag else "observation"

    @property
    def clinical_review_required(self) -> bool:
        return self.condition_flag


# Only explicit disease/name phrases may produce condition tags.  Keep this
# separate from the display label so words such as 「紅」「腫塊」 cannot infer a
# diagnosis-like tag.
CONDITION_ALIASES: dict[str, tuple[str, ...]] = {
    "seborrheic_dermatitis_pattern": ("脂漏性皮膚炎", "脂溢性皮膚炎"),
    "lipoma_like_lump": ("脂肪瘤",),
    "stye_pattern": ("麥粒腫", "針眼"),
    "chalazion_pattern": ("霰粒腫",),
    "atopic_dermatitis_pattern": ("異位性皮膚炎",),
    "allergic_contact_dermatitis_pattern": ("過敏性接觸皮膚炎",),
    "irritant_contact_dermatitis_pattern": ("刺激性接觸皮膚炎",),
    "rosacea_pattern": ("酒糟", "玫瑰斑"),
    "facial_psoriasis_pattern": ("乾癬",),
    "urticarial_wheals": ("蕁麻疹",),
    "angular_cheilitis_pattern": ("口角炎",),
    "cold_sore_pattern": ("唇疱疹", "唇皰疹"),
    "impetigo_pattern": ("膿痂疹",),
    "facial_shingles_pattern": ("帶狀疱疹", "帶狀皰疹"),
}


OBSERVATION_ALIASES: dict[str, tuple[str, ...]] = {
    "skin_dryness": ("乾燥", "缺水"),
    "skin_flaking": ("脫皮", "起皮", "脫屑"),
    "skin_oiliness": ("出油", "油光"),
    "rough_texture": ("粗糙",),
    "uneven_texture": ("凹凸不平", "顆粒感", "小顆粒", "凸起"),
    "enlarged_pore_appearance": ("毛孔粗大", "毛孔明顯"),
    "closed_comedones": ("閉鎖性粉刺", "白頭粉刺"),
    "open_comedones": ("黑頭粉刺", "黑頭"),
    "inflammatory_papules": ("發炎丘疹", "紅丘疹"),
    "pustules": ("膿皰",),
    "atrophic_acne_scars": ("凹痘疤", "凹陷痘疤"),
    "post_acne_erythema": ("痘後紅印", "紅痘印"),
    "post_acne_hyperpigmentation": ("痘後色素沉澱", "黑痘印"),
    "diffuse_facial_redness": ("泛紅", "紅紅的", "臉紅"),
    "episodic_flushing": ("潮紅",),
    "visible_telangiectasia": ("血絲", "微血管"),
    "localized_pruritus": ("搔癢", "發癢", "皮膚癢", "癢癢的"),
    "uneven_skin_tone": ("膚色不均",),
    "hyperpigmented_macules": ("色素沉澱", "深色斑點"),
    "freckle_like_macules": ("雀斑",),
    "periorbital_darkening": ("黑眼圈", "眼周暗沉"),
    "under_eye_puffiness": ("眼袋", "眼下浮腫"),
    "tear_trough_hollow": ("淚溝",),
    "fine_lines": ("細紋",),
    "deep_wrinkles": ("皺紋",),
    "under_eye_crepey_texture": ("眼下紙紋", "眼下薄皺"),
    "eyelid_redness": ("眼瞼泛紅", "眼皮泛紅"),
    "eyelid_margin_scale_crust": ("眼瞼邊緣皮屑", "眼瞼邊緣結痂"),
    "chapped_lips": ("嘴唇乾裂", "唇乾"),
    "lip_scaling_peeling": ("嘴唇脫皮", "唇部脫皮"),
    "lip_fissure": ("唇部裂口", "嘴唇裂口"),
}


REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "whole_face": ("全臉", "整張臉", "整臉"),
    "hairline": ("髮際線",),
    "forehead": ("額頭",),
    "glabella": ("眉心",),
    "eyebrow": ("眉毛", "眉部"),
    "temple": ("太陽穴",),
    "nose_bridge": ("鼻樑",),
    "nasal_ala": ("鼻翼", "鼻翼兩側"),
    "nose_tip": ("鼻尖", "鼻頭"),
    "nostril": ("鼻孔",),
    "nose": ("鼻子",),
    "cheek": ("左臉頰", "右臉頰", "臉頰", "兩頰", "雙頰"),
    "jawline": ("下顎線", "下頜線"),
    "chin": ("下巴",),
    "upper_eyelid": ("上眼瞼", "上眼皮"),
    "lower_eyelid": ("下眼瞼", "下眼皮"),
    "eyelid_margin": ("眼瞼邊緣", "瞼緣"),
    "under_eye": ("眼下", "眼睛下面"),
    "inner_eye_corner": ("眼頭", "內眼角"),
    "outer_eye_corner": ("眼尾", "外眼角"),
    "periorbital": ("眼周",),
    "upper_lip": ("上唇",),
    "lower_lip": ("下唇",),
    "lip_border": ("唇緣",),
    "mouth_corner": ("嘴角", "口角"),
    "perioral": ("嘴周", "口周"),
    "nasolabial_fold": ("法令紋", "鼻唇溝"),
    "beard_area": ("鬍鬚區", "鬍子區"),
    "ear": ("耳朵", "耳周"),
}


@lru_cache(maxsize=1)
def load_catalog() -> tuple[ObservationTag, ...]:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    rows = payload.get("labels")
    if not isinstance(rows, list):
        raise ValueError("skin observation catalog must contain a labels list")
    tags = tuple(
        ObservationTag(
            slug=str(row["slug"]),
            name_zh_tw=str(row["name_zh_tw"]),
            category=str(row["category"]),
            face_regions=tuple(str(value) for value in row.get("face_regions", [])),
            condition_flag=bool(row.get("condition_flag")),
            source_urls=tuple(str(value) for value in row.get("source_urls", [])),
        )
        for row in rows
    )
    slugs = [tag.slug for tag in tags]
    if len(tags) < 100 or len(set(slugs)) != len(slugs):
        raise ValueError("skin observation catalog requires at least 100 unique slugs")
    return tags


@lru_cache(maxsize=1)
def catalog_by_slug() -> dict[str, ObservationTag]:
    return {tag.slug: tag for tag in load_catalog()}


@lru_cache(maxsize=1)
def allowed_detail_zones() -> frozenset[str]:
    """Return every stable region code accepted by extraction and catalog data.

    REGION_ALIASES is the deterministic text-extraction vocabulary.  Its keys
    must remain valid even when the current observation catalog does not happen
    to reference a particular region.
    """
    catalog_regions = frozenset(
        region for tag in load_catalog() for region in tag.face_regions
    )
    return frozenset(REGION_ALIASES) | catalog_regions


def validate_tag_slugs(values: Iterable[str] | None) -> list[str]:
    values = list(values or [])
    known = catalog_by_slug()
    normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    unknown = [value for value in normalized if value not in known]
    if unknown:
        raise ValueError(f"unknown skin observation tag slug(s): {', '.join(unknown)}")
    return normalized


def validate_detail_zones(values: Iterable[str] | None) -> list[str]:
    values = list(values or [])
    allowed = allowed_detail_zones()
    normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    unknown = [value for value in normalized if value not in allowed]
    if unknown:
        raise ValueError(f"unknown face detail zone(s): {', '.join(unknown)}")
    return normalized


def tag_label(slug: str) -> str:
    tag = catalog_by_slug().get(slug)
    return tag.name_zh_tw if tag else slug


def extract_observation_metadata(
    message: str,
    *,
    suggested_tag_slugs: Iterable[str] | None = None,
    suggested_detail_zones: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return validated tags/zones, suppressing non-explicit condition suggestions."""
    text = (message or "").strip()
    catalog = catalog_by_slug()
    explicit_catalog_names = {
        slug
        for slug, tag in catalog.items()
        if tag.name_zh_tw and tag.name_zh_tw in text
    }
    explicit_conditions = {
        slug
        for slug, aliases in CONDITION_ALIASES.items()
        if any(alias in text for alias in aliases)
    } | {
        slug for slug in explicit_catalog_names if catalog[slug].condition_flag
    }
    deterministic_observations = {
        slug
        for slug, aliases in OBSERVATION_ALIASES.items()
        if any(alias in text for alias in aliases)
    } | {
        slug for slug in explicit_catalog_names if not catalog[slug].condition_flag
    }

    accepted_suggestions = set()
    for slug in validate_tag_slugs(suggested_tag_slugs):
        tag = catalog[slug]
        if not tag.condition_flag or slug in explicit_conditions:
            accepted_suggestions.add(slug)

    tags = validate_tag_slugs(
        sorted(explicit_conditions | deterministic_observations | accepted_suggestions)
    )
    detected_zones = {
        region
        for region, aliases in REGION_ALIASES.items()
        if any(alias in text for alias in aliases)
    }
    zones = validate_detail_zones(
        [*(suggested_detail_zones or []), *sorted(detected_zones)]
    )
    return tags, zones


def catalog_seed_payload() -> list[dict]:
    """Neo4j-ready catalog rows; source metadata remains lossless."""
    return [
        {
            "slug": tag.slug,
            "name_zh_tw": tag.name_zh_tw,
            "category": tag.category,
            "kind": tag.kind,
            "condition_flag": tag.condition_flag,
            "clinical_review_required": tag.clinical_review_required,
            "source_urls": list(tag.source_urls),
            "face_regions": list(tag.face_regions),
            "aliases": list(CONDITION_ALIASES.get(tag.slug, ()))
            + list(OBSERVATION_ALIASES.get(tag.slug, ())),
        }
        for tag in load_catalog()
    ]
