"""Validated domain models shared by the Skin Tracking feature."""
from __future__ import annotations

from datetime import date as Date
from enum import Enum
from typing import ClassVar, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from agents.skin_tracking.observation_taxonomy import (
    validate_detail_zones,
    validate_tag_slugs,
)


class Issue(str, Enum):
    acne = "acne"
    dryness = "dryness"
    redness = "redness"
    oiliness = "oiliness"
    itching = "itching"
    pigmentation = "pigmentation"
    acne_scar = "acne_scar"
    dark_spot = "dark_spot"
    other = "other"


class Zone(str, Enum):
    t_zone = "t_zone"            # 額頭+鼻樑+鼻翼+鼻頭
    left_cheek = "left_cheek"    # 左眼周+左臉頰
    right_cheek = "right_cheek"  # 右眼周+右臉頰
    chin = "chin"                # 嘴周+人中+下巴


ZONE_LABELS = {
    Zone.t_zone: "T字部位（額頭、鼻子）",
    Zone.left_cheek: "左臉頰區（含左眼周）",
    Zone.right_cheek: "右臉頰區（含右眼周）",
    Zone.chin: "下巴區（嘴周、人中、下巴）",
}

ISSUE_LABELS = {
    Issue.acne: "痘痘", Issue.dryness: "乾燥", Issue.redness: "泛紅",
    Issue.oiliness: "出油", Issue.itching: "搔癢",
    Issue.pigmentation: "斑點", Issue.acne_scar: "痘疤",
    Issue.dark_spot: "斑點/痘疤（舊紀錄）", Issue.other: "其他",
}

SELECTABLE_ISSUES = (
    Issue.acne,
    Issue.dryness,
    Issue.redness,
    Issue.oiliness,
    Issue.itching,
    Issue.pigmentation,
    Issue.acne_scar,
    Issue.other,
)


class TrackIntent(str, Enum):
    create = "create"
    query = "query"
    update = "update"
    delete = "delete"
    report = "report"
    unknown = "unknown"


class RecordStatus(str, Enum):
    recorded = "recorded"
    stable = "stable"


class StabilitySource(str, Enum):
    user_reported = "user_reported"
    agent_confirmed = "agent_confirmed"


RECORD_STATUS_LABELS = {
    RecordStatus.recorded: "已記錄",
    RecordStatus.stable: "穩定",
}


class RecordDraft(BaseModel):
    """slot-filling 草稿；None = 尚未取得"""
    intent: TrackIntent = TrackIntent.unknown
    issue: Optional[Issue] = None
    severity: Optional[int] = Field(None, ge=1, le=5)
    zone: Optional[Zone] = None
    tag_slugs: list[str] = Field(default_factory=list)
    detail_zones: list[str] = Field(default_factory=list)
    onset_date: Optional[Date] = None
    duration_days: Optional[int] = Field(None, ge=0)
    products: list[str] = Field(default_factory=list)
    note: str = ""
    record_status: RecordStatus = RecordStatus.recorded
    stability_source: Optional[StabilitySource] = None

    # 順序即追問順序
    REQUIRED: ClassVar[tuple[str, ...]] = ("issue", "zone", "severity")

    def missing_slots(self) -> list[str]:
        # A useful daily record only needs the concern, location and intensity.
        # Time and products are still extracted when volunteered, but are no longer
        # forced as questionnaire steps.
        return [s for s in self.REQUIRED if getattr(self, s) is None]

    @field_validator("severity", "duration_days", mode="before")
    @classmethod
    def _coerce_int(cls, v):
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v

    @field_validator("tag_slugs", mode="before")
    @classmethod
    def _validate_tag_slugs(cls, value):
        return validate_tag_slugs(value)

    @field_validator("detail_zones", mode="before")
    @classmethod
    def _validate_detail_zones(cls, value):
        return validate_detail_zones(value)

    @model_validator(mode="after")
    def _keep_stability_fields_consistent(self):
        """Stable records require provenance; ordinary records never retain it."""
        if self.record_status == RecordStatus.stable and self.stability_source is None:
            raise ValueError("stable records require stability_source")
        if self.record_status == RecordStatus.recorded:
            self.stability_source = None
        return self


class ZoneStats(BaseModel):
    zone: Zone
    record_count: int = 0
    top_issue: Optional[Issue] = None
    avg_severity: Optional[float] = None
    max_severity: Optional[int] = None
    trend: Optional[str] = None  # improving | stable | worsening
    products: list[str] = Field(default_factory=list)


class TagStats(BaseModel):
    slug: str
    name_zh_tw: str
    kind: str
    count: int = 0


class SkinReport(BaseModel):
    user_id: str
    date_from: Date
    date_to: Date
    consecutive_days: int
    zones: list[ZoneStats]
    worst_zone: Optional[Zone] = None
    top_tags: list[TagStats] = Field(default_factory=list)
    summary_text: str = ""

class ExportSkinContextInput(BaseModel):
    """膚況 Context A2A 出口的輸入（export_skin_context_tool 用）"""
    user_id: str
    period_days: int = 30
    purpose: str = "product_recommendation"
