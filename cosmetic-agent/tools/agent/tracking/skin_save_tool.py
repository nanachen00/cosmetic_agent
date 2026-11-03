"""
Skin Save Tool
檔案路徑：tools/agent/tracking/skin_save_tool.py
職責：將確認無誤的膚況草稿寫入 Neo4j 資料庫，並檢查連續紀錄天數與報表資格
"""

from datetime import date as Date, timedelta
from typing import Dict, Any, Optional

from schemas.tracking_schema import RecordDraft
from tools.agent.tracking.skin_record_tool import SkinRecordStore

MIN_REPORT_DAYS = 5


def _to_str(val: Any) -> str:
    """安全的轉型函式：若為 Enum 則取 .value，若為字串則直接回傳，Null 則回傳 'other'"""
    if val is None:
        return "other"
    return val.value if hasattr(val, "value") else str(val)


def save_skin_record(
    store: Optional[SkinRecordStore], 
    user_id: str, 
    draft_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """
    [Tool] 將膚況草稿轉換為標準結構並存入 Neo4j，同時計算連勝天數與解鎖狀態
    store 必須由 application/service 層注入，避免 tool 偷建連線或使用 fallback 密碼。
    """
    if store is None:
        raise ValueError("SkinRecordStore is required")

    raw_record_date = draft_dict.get("record_date")
    record_date = (
        Date.fromisoformat(str(raw_record_date))
        if raw_record_date
        else Date.today()
    )
    d = RecordDraft(
        **{key: value for key, value in draft_dict.items() if key != "record_date"}
    )

    onset = d.onset_date
    if onset is None and d.duration_days is not None:
        onset = Date.today() - timedelta(days=d.duration_days)

    record_id = store.create(user_id, {
        "date": record_date,
        "issue": _to_str(d.issue),
        "severity": d.severity,
        "zone": _to_str(d.zone),
        "tag_slugs": d.tag_slugs,
        "detail_zones": d.detail_zones,
        "onset_date": onset,
        "duration_days": d.duration_days,
        "products": d.products or [],
        "note": d.note or "",
        "record_status": d.record_status.value,
        "stability_source": d.stability_source.value if d.stability_source else None,
    })

    elig = dict(
        store.check_eligibility(
            user_id,
            record_date,
            min_days=MIN_REPORT_DAYS,
        )
        or {}
    )
    elig["eligible"] = True
    elig["days_needed"] = 0

    return {
        "record_id": record_id,
        "message": "好，今天的膚況我幫你收好了",
        "quick_replies": [{"label": "前往膚況分析報告", "value": "open_skin_report"}],
        "eligibility": elig,
    }
