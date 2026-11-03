"""Business operations for legacy-compatible skin record APIs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date
from typing import Any


_REGION_TO_ZONE = {
    "t_zone": "t_zone",
    "left_cheek": "left_cheek",
    "right_cheek": "right_cheek",
    "chin": "chin",
    "T 字部位": "t_zone",
    "T字部位": "t_zone",
    "額頭": "t_zone",
    "眉心": "t_zone",
    "鼻樑": "t_zone",
    "鼻翼": "t_zone",
    "鼻頭": "t_zone",
    "左眼周": "left_cheek",
    "左顴骨": "left_cheek",
    "左臉頰": "left_cheek",
    "右眼周": "right_cheek",
    "右顴骨": "right_cheek",
    "右臉頰": "right_cheek",
    "人中": "chin",
    "嘴角": "chin",
    "下唇周圍": "chin",
    "下巴": "chin",
}
_CONDITION_TO_ISSUE = {
    "acne": "acne",
    "dryness": "dryness",
    "redness": "redness",
    "oiliness": "oiliness",
    "itching": "itching",
    "pigmentation": "pigmentation",
    "acne_scar": "acne_scar",
    "other": "other",
    "tightness": "dryness",
    "redness_feeling": "redness",
    "roughness": "dryness",
    "sticky_feeling": "oiliness",
    "stable": "other",
    "痘痘": "acne",
    "乾燥": "dryness",
    "泛紅": "redness",
    "出油": "oiliness",
    "搔癢": "itching",
    "斑點／痘疤": "pigmentation",
    "斑點/痘疤": "pigmentation",
    "斑點": "pigmentation",
    "痘疤": "acne_scar",
    "其他": "other",
}


class SkinRecordService:
    def __init__(
        self,
        *,
        store_provider: Callable[[], Any],
        save_record: Callable[..., Any],
        build_report: Callable[..., Any],
        render_report_text: Callable[[Any], str],
    ) -> None:
        self._store_provider = store_provider
        self._save_record = save_record
        self._build_report = build_report
        self._render_report_text = render_report_text

    def create(
        self,
        *,
        user_id: str,
        issue: str,
        zone: str,
        severity: int,
        note: str,
        products: list[str],
        duration_days: int | None,
    ) -> dict[str, Any]:
        draft = {
            "intent": "create",
            "issue": issue,
            "zone": zone,
            "severity": severity,
            "note": note,
            "products": products,
            "duration_days": duration_days,
        }
        result = self._save_record(self._store_provider(), user_id, draft)
        return result or {}

    def list(
        self,
        *,
        user_id: str,
        date_from: str | None,
        date_to: str | None,
    ) -> list[dict[str, Any]]:
        store = self._store_provider()
        try:
            return store.query(user_id, date_from=date_from, date_to=date_to)
        except TypeError:
            return store.query(user_id, date_from=date_from)

    def patch(self, *, user_id: str, record_id: str, fields: dict[str, Any]) -> bool:
        return bool(self._store_provider().update(user_id, record_id, fields))

    def delete(self, *, user_id: str, record_id: str) -> bool:
        return bool(self._store_provider().delete(user_id, record_id))

    def eligibility(self, *, user_id: str) -> dict[str, Any]:
        progress = dict(
            self._store_provider().check_eligibility(user_id, min_days=5) or {}
        )
        progress["eligible"] = True
        progress["days_needed"] = 0
        return progress

    def build_current_report(self, *, user_id: str) -> tuple[str, Any]:
        store = self._store_provider()
        records = store.query(user_id)
        report = self._build_report(
            user_id=user_id,
            records=records,
            llm_client=None,
        )
        try:
            text = self._render_report_text(report)
        except Exception:
            text = str(report)
        return text, report

    def create_from_frontend(
        self,
        *,
        user_id: str,
        region: str,
        conditions: list[str],
        severity: int,
        notes: str,
        record_date: str | None = None,
    ) -> dict[str, Any]:
        issue = _CONDITION_TO_ISSUE.get(conditions[0], conditions[0]) if conditions else "stable"
        extra = "、".join(conditions[1:]) if len(conditions) > 1 else ""
        note = notes + (f"（另有：{extra}）" if extra else "")
        draft = {
            "intent": "create",
            "issue": issue,
            "zone": _REGION_TO_ZONE.get(region, region),
            "severity": severity,
            "note": note,
            "products": [],
        }
        if record_date:
            Date.fromisoformat(record_date)
            draft["record_date"] = record_date
        result = self._save_record(self._store_provider(), user_id, draft)
        return result or {}
