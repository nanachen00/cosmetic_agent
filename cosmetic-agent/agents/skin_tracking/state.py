"""
Skin Tracking State v2
对話狀態機使用的 session 物件，透過 dict 在多輪对話間序列化傳這。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class SkinSession:
    """单一使用者的膚況紀錄對話狀態。"""

    phase: str = "collect"
    draft: dict = field(default_factory=dict)
    drafts: list[dict] = field(default_factory=list)
    last_listed: list = field(default_factory=list)
    pending_action: Optional[str] = None
    pending_record_id: Optional[int] = None
    pending_fields: Optional[dict] = None
    pending_stability_draft_indexes: list[int] = field(default_factory=list)
    concern_assessments: dict[str, dict] = field(default_factory=dict)
    pending_location: Optional[dict] = None

    @classmethod
    def from_dict(cls, session: Optional[dict]) -> "SkinSession":
        if not session:
            return cls()
        ss = cls()
        for k in (
            "phase",
            "draft",
            "drafts",
            "last_listed",
            "pending_action",
            "pending_record_id",
            "pending_fields",
            "pending_stability_draft_indexes",
            "concern_assessments",
            "pending_location",
        ):
            if k in session and session[k] is not None:
                setattr(ss, k, session[k])
        return ss

    def to_dict(self) -> dict:
        return asdict(self)
