"""Deterministic face-location semantics for the skin-record flow.

The four Zone values remain the storage/reporting contract. This module adds a
finer-grained anatomical layer and keeps button actions separate from free-text
interpretation.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from schemas.tracking_schema import Zone


_ZONE_ORDER = (Zone.t_zone, Zone.left_cheek, Zone.right_cheek, Zone.chin)
_SIDE_WORDS = {
    "left": ("左邊", "左側", "左面", "左"),
    "right": ("右邊", "右側", "右面", "右"),
    "bilateral": ("左右兩邊", "左右兩側", "左右", "兩邊", "兩側", "雙邊", "雙側", "兩面"),
}


@dataclass(frozen=True)
class RegionSpec:
    aliases: tuple[str, ...]
    detail_zones: tuple[str, ...]
    label: str
    macro_zones: tuple[Zone, ...] = ()
    side_dependent: bool = False


# These codes reuse the existing 100-label catalogue's face_regions values.
# Generic anatomy such as 眼瞼/鼻翼 is represented by the nearest controlled
# code(s), while label preserves the wording shown to the user.
REGIONS: dict[str, RegionSpec] = {
    "whole_face": RegionSpec(("整張臉", "整臉", "全臉", "整個臉"), ("whole_face",), "全臉", _ZONE_ORDER),
    "hairline": RegionSpec(("髮際線", "髮線"), ("hairline",), "髮際線", (Zone.t_zone,)),
    "forehead": RegionSpec(("額頭", "前額"), ("forehead",), "額頭", (Zone.t_zone,)),
    "glabella": RegionSpec(("眉心", "眉間"), ("glabella",), "眉心", (Zone.t_zone,)),
    "eyebrow": RegionSpec(("眉毛周圍", "眉毛", "眉骨"), ("eyebrow",), "眉部", side_dependent=True),
    "temple": RegionSpec(("太陽穴", "顳部", "鬢角"), ("temple",), "太陽穴", side_dependent=True),
    "nose_bridge": RegionSpec(("鼻樑", "鼻梁", "山根"), ("nose_bridge",), "鼻樑", (Zone.t_zone,)),
    "nasal_ala": RegionSpec(("鼻翼兩側", "鼻翼旁邊", "鼻翼旁", "鼻翼"), ("nasal_ala",), "鼻翼", (Zone.t_zone,)),
    "nose_tip": RegionSpec(("鼻尖", "鼻頭"), ("nose_tip",), "鼻頭", (Zone.t_zone,)),
    "nostril": RegionSpec(("鼻孔周圍", "鼻孔"), ("nostril",), "鼻孔周圍", (Zone.t_zone,)),
    "nose": RegionSpec(("T字部位", "T字區", "T字", "鼻子"), ("nose",), "T字／鼻部", (Zone.t_zone,)),
    "upper_eyelid": RegionSpec(("上眼瞼", "上眼皮"), ("upper_eyelid",), "上眼瞼", side_dependent=True),
    "lower_eyelid": RegionSpec(("下眼瞼", "下眼皮"), ("lower_eyelid",), "下眼瞼", side_dependent=True),
    "eyelid_margin": RegionSpec(("睫毛根部", "眼瞼邊緣", "眼瞼緣"), ("eyelid_margin",), "眼瞼邊緣", side_dependent=True),
    "eyelid": RegionSpec(("眼瞼", "眼皮"), ("upper_eyelid", "lower_eyelid"), "眼瞼", side_dependent=True),
    "under_eye": RegionSpec(("眼睛下面", "眼睛下方", "眼下", "淚溝"), ("under_eye",), "眼下", side_dependent=True),
    "inner_eye_corner": RegionSpec(("內眼角", "眼頭"), ("inner_eye_corner",), "內眼角", side_dependent=True),
    "outer_eye_corner": RegionSpec(("外眼角", "眼尾"), ("outer_eye_corner",), "眼尾", side_dependent=True),
    "periorbital": RegionSpec(("眼睛周圍", "眼睛週圍", "眼周圍", "眼周", "眼睛附近"), ("periorbital",), "眼周", side_dependent=True),
    "cheek": RegionSpec(("左右臉頰", "左右兩頰", "兩邊臉頰", "雙頰", "兩頰", "左臉", "右臉", "臉頰", "面頰"), ("cheek",), "臉頰", side_dependent=True),
    "jawline": RegionSpec(("下顎線", "下顎", "下頷線", "輪廓線"), ("jawline",), "下顎線", (Zone.chin,)),
    "chin": RegionSpec(("下巴", "頦部"), ("chin",), "下巴", (Zone.chin,)),
    "philtrum": RegionSpec(("人中",), ("perioral",), "人中", (Zone.chin,)),
    "nasolabial_fold": RegionSpec(("法令紋", "鼻唇溝"), ("nasolabial_fold",), "法令紋", (Zone.chin,)),
    "upper_lip": RegionSpec(("上嘴唇", "上唇"), ("upper_lip",), "上唇", (Zone.chin,)),
    "lower_lip": RegionSpec(("下嘴唇", "下唇"), ("lower_lip",), "下唇", (Zone.chin,)),
    "lip_border": RegionSpec(("唇緣", "嘴唇邊緣"), ("lip_border",), "唇緣", (Zone.chin,)),
    "mouth_corner": RegionSpec(("嘴角", "口角"), ("mouth_corner",), "嘴角", (Zone.chin,)),
    "perioral": RegionSpec(("嘴巴周圍", "嘴周", "口周"), ("perioral",), "嘴周", (Zone.chin,)),
    "ear": RegionSpec(("耳朵", "耳周", "耳前"), ("ear",), "耳周", side_dependent=True),
}


@dataclass(frozen=True)
class FaceLocation:
    region: str
    laterality: str
    label: str
    detail_zones: tuple[str, ...]
    macro_zones: tuple[Zone, ...]

    def to_pending_dict(self) -> dict:
        return {"region": self.region, "label": self.label}


@dataclass(frozen=True)
class LocationParse:
    locations: tuple[FaceLocation, ...] = ()

    @property
    def recognized(self) -> bool:
        return bool(self.locations)

    @property
    def unresolved(self) -> tuple[FaceLocation, ...]:
        return tuple(item for item in self.locations if not item.macro_zones)

    @property
    def needs_laterality(self) -> bool:
        return bool(self.unresolved)

    @property
    def pending(self) -> dict | None:
        unresolved = self.unresolved
        if not unresolved:
            return None
        return {"locations": [item.to_pending_dict() for item in unresolved]}

    @property
    def clarification(self) -> str:
        labels = "、".join(dict.fromkeys(item.label for item in self.unresolved))
        return f"你說的{labels or '部位'}是左邊、右邊，還是兩邊都有呢？"


def _normalize(text: str) -> str:
    return re.sub(r"[\s　]+", "", str(text or "")).lower()


def _laterality(text: str, start: int, end: int, alias: str) -> str:
    # Keep the context local so a previous clause's side cannot leak into this
    # location (for example: 不是左臉，是右臉).
    local = text[max(0, start - 2) : min(len(text), end + 2)]
    if alias.startswith(("兩", "雙")) or any(word in local or word in alias for word in _SIDE_WORDS["bilateral"]):
        return "bilateral"
    left = "左" in local or "左" in alias
    right = "右" in local or "右" in alias
    if left and right:
        return "bilateral"
    if left:
        return "left"
    if right:
        return "right"
    return "unspecified"


def _is_negated(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 4) : start]
    suffix = text[end : min(len(text), end + 6)]
    return any(word in prefix for word in ("不是", "並非", "不要記")) or any(
        word in suffix for word in ("沒事", "沒有問題", "正常", "不用記")
    )


def _macro_zones(spec: RegionSpec, side: str) -> tuple[Zone, ...]:
    if not spec.side_dependent:
        return spec.macro_zones
    if side == "left":
        return (Zone.left_cheek,)
    if side == "right":
        return (Zone.right_cheek,)
    if side == "bilateral":
        return (Zone.left_cheek, Zone.right_cheek)
    return ()


def _display_label(spec: RegionSpec, side: str) -> str:
    if side == "left":
        return f"左{spec.label}"
    if side == "right":
        return f"右{spec.label}"
    if side == "bilateral":
        return f"雙側{spec.label}"
    return spec.label


def extract_face_locations(text: str) -> LocationParse:
    """Extract detailed regions and their four-zone mapping from free text."""

    normalized = _normalize(text)
    candidates: list[tuple[int, int, str, str]] = []
    for region, spec in REGIONS.items():
        for alias in spec.aliases:
            offset = 0
            while True:
                start = normalized.find(alias.lower(), offset)
                if start < 0:
                    break
                candidates.append((start, start + len(alias), region, alias))
                offset = start + 1

    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    accepted: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        start, end, _, _ = candidate
        if any(start < used_end and end > used_start for used_start, used_end, _, _ in accepted):
            continue
        if _is_negated(normalized, start, end):
            continue
        accepted.append(candidate)

    locations: list[FaceLocation] = []
    seen: set[tuple[str, str]] = set()
    for start, end, region, alias in sorted(accepted):
        spec = REGIONS[region]
        side = _laterality(normalized, start, end, alias)
        key = (region, side)
        if key in seen:
            continue
        seen.add(key)
        locations.append(FaceLocation(
            region=region,
            laterality=side,
            label=_display_label(spec, side),
            detail_zones=spec.detail_zones,
            macro_zones=_macro_zones(spec, side),
        ))
    return LocationParse(tuple(locations))


def resolve_pending_location(pending: dict | None, reply: str) -> LocationParse:
    """Resolve a previous side-ambiguous region using a side-only reply."""

    if not pending:
        return LocationParse()
    normalized = _normalize(reply)
    side = ""
    if any(word in normalized for word in _SIDE_WORDS["bilateral"]):
        side = "bilateral"
    elif any(word in normalized for word in _SIDE_WORDS["left"]):
        side = "left"
    elif any(word in normalized for word in _SIDE_WORDS["right"]):
        side = "right"
    if not side:
        return LocationParse()

    resolved: list[FaceLocation] = []
    for item in pending.get("locations") or []:
        region = str(item.get("region") or "")
        spec = REGIONS.get(region)
        if not spec:
            continue
        resolved.append(FaceLocation(
            region=region,
            laterality=side,
            label=_display_label(spec, side),
            detail_zones=spec.detail_zones,
            macro_zones=_macro_zones(spec, side),
        ))
    return LocationParse(tuple(resolved))


def zones_from_action(value: str) -> list[Zone]:
    """Parse trusted structured button values without semantic inference."""

    label_to_zone = {
        "T字部位（額頭、鼻子）": Zone.t_zone,
        "左臉頰區（含左眼周）": Zone.left_cheek,
        "右臉頰區（含右眼周）": Zone.right_cheek,
        "下巴區（嘴周、人中、下巴）": Zone.chin,
    }
    result: list[Zone] = []
    exact = label_to_zone.get(str(value or "").strip())
    if exact:
        return [exact]

    for token in re.split(r"[、,，|\s]+", str(value or "").strip()):
        if not token:
            continue
        try:
            zone = Zone(token)
        except ValueError:
            zone = label_to_zone.get(token)
        if zone and zone not in result:
            result.append(zone)
    return result


_DETAIL_LABELS = {
    "whole_face": "全臉", "hairline": "髮際線", "forehead": "額頭",
    "glabella": "眉心", "eyebrow": "眉部", "temple": "太陽穴",
    "nose_bridge": "鼻樑", "nasal_ala": "鼻翼", "nose_tip": "鼻頭", "nostril": "鼻孔周圍", "nose": "鼻部",
    "cheek": "臉頰", "jawline": "下顎線", "chin": "下巴",
    "upper_eyelid": "上眼瞼", "lower_eyelid": "下眼瞼",
    "eyelid_margin": "眼瞼邊緣", "under_eye": "眼下",
    "inner_eye_corner": "內眼角", "outer_eye_corner": "眼尾",
    "periorbital": "眼周", "upper_lip": "上唇", "lower_lip": "下唇",
    "lip_border": "唇緣", "mouth_corner": "嘴角", "perioral": "嘴周／人中",
    "nasolabial_fold": "法令紋", "beard_area": "鬍鬚區", "ear": "耳周",
}
_LATERAL_DETAIL_ZONES = {
    "eyebrow", "temple", "cheek", "upper_eyelid", "lower_eyelid",
    "eyelid_margin", "under_eye", "inner_eye_corner", "outer_eye_corner",
    "periorbital", "ear",
}


def detail_location_labels(zone: Zone | str | None, detail_zones: Iterable[str]) -> list[str]:
    """Render storage-safe detail codes as user-facing anatomical tags."""

    try:
        zone_value = zone if isinstance(zone, Zone) else Zone(str(zone))
    except (TypeError, ValueError):
        zone_value = None
    details = list(dict.fromkeys(str(item) for item in detail_zones if item))
    collapse_eyelid = "upper_eyelid" in details and "lower_eyelid" in details
    if collapse_eyelid:
        details = [item for item in details if item not in {"upper_eyelid", "lower_eyelid"}]
        details.append("eyelid")

    labels: list[str] = []
    for code in details:
        label = "眼瞼" if code == "eyelid" else _DETAIL_LABELS.get(code, code)
        if code in _LATERAL_DETAIL_ZONES or code == "eyelid":
            if zone_value == Zone.left_cheek:
                label = f"左{label}"
            elif zone_value == Zone.right_cheek:
                label = f"右{label}"
        if label not in labels:
            labels.append(label)
    return labels


def merge_detail_zones(existing: Iterable[str], additions: Iterable[str]) -> list[str]:
    values = [*(str(item) for item in existing or []), *(str(item) for item in additions or [])]
    return list(dict.fromkeys(values))
