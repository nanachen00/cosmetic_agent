"""將新版 Neo4j SkinRecord 匯出成商品 Agent 可使用的安全膚況 context。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as Date, timedelta

from schemas.tracking_schema import ExportSkinContextInput
from tools.agent.tracking.skin_record_tool import SkinRecordStore


def _trend(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        issue = record.get("issue")
        if issue:
            grouped[issue].append(record)

    trends = []
    for issue, items in grouped.items():
        ordered = sorted(
            (item for item in items if item.get("severity") is not None),
            key=lambda item: str(item.get("date", "")),
        )
        if len(ordered) < 2:
            direction = "stable"
        else:
            delta = ordered[-1]["severity"] - ordered[0]["severity"]
            direction = "increasing" if delta > 0 else "decreasing" if delta < 0 else "stable"
        trends.append({"condition": issue, "trend": direction, "record_count": len(items)})
    return sorted(trends, key=lambda item: item["record_count"], reverse=True)


def export_skin_context_tool(_db, data: ExportSkinContextInput, store=None) -> dict:
    """保留既有 A2A function signature，但正式資料只讀 Neo4j SkinRecord。"""
    owns_store = store is None
    store = store or SkinRecordStore()
    try:
        today = Date.today()
        records = store.query(
            data.user_id,
            str(today - timedelta(days=data.period_days - 1)),
            str(today),
        )
    finally:
        if owns_store:
            store.close()

    issue_counts = Counter(r.get("issue") for r in records if r.get("issue"))
    zone_counts = Counter(r.get("zone") for r in records if r.get("zone"))
    total = max(len(records), 1)
    current_skin_signals = {
        issue: round(count / total, 2) for issue, count in issue_counts.items()
    }

    context = {
        "user_id": data.user_id,
        "purpose": data.purpose,
        "skin_records_summary": {
            "period_days": data.period_days,
            "record_count": len(records),
            "frequent_conditions": [name for name, _ in issue_counts.most_common(5)],
            "frequent_areas": [name for name, _ in zone_counts.most_common(5)],
            "condition_counts": dict(issue_counts),
            "area_counts": dict(zone_counts),
        },
        "user_skin_profile": {
            "user_id": data.user_id,
            "period_days": data.period_days,
            "current_skin_signals": current_skin_signals,
            "frequent_areas": [name for name, _ in zone_counts.most_common(5)],
            "recent_trends": _trend(records),
            "compliance_note": (
                "This context summarizes user-reported skin records and does not "
                "diagnose conditions or infer medical causes."
            ),
        },
    }
    return {"ok": True, "skin_context": context}
