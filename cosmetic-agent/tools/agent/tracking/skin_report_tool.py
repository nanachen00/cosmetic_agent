"""
Skin Report Tool — 膚況數據統計與文字報表渲染工具（無分數版）
檔案路徑：tools/agent/tracking/report_tool.py
"""
from __future__ import annotations

from collections import Counter
from datetime import date as Date
import json
from typing import List, Dict, Any, Optional

from agents.skin_tracking.location_semantics import detail_location_labels
from agents.skin_tracking.observation_taxonomy import catalog_by_slug, tag_label
from schemas.tracking_schema import (
    ISSUE_LABELS,
    ZONE_LABELS,
    Issue,
    SkinReport,
    TagStats,
    Zone,
    ZoneStats,
)

TREND_LABELS = {
    "improving": "改善中 ↓",
    "stable": "持平 →",
    "worsening": "惡化中 ↑",
}

SYSTEM_PROMPT = """你是 BLINK AI 的專業美妝助理，像一位細心、懂保養的朋友。請把跨日紀錄整理成一眼能看懂、真的能採取行動的重點，不可逐筆複述或改寫使用者原話。

【可使用的證據】
{report_evidence}

【分析原則】
1. 先比較不同日期、部位、症狀、程度與產品紀錄，辨認持續、反覆、改善、惡化及左右差異。
2. 只有資料中反覆同時出現的因素才能寫成「可能相關」；不可宣稱因果，也不可補上使用者未提供的作息、飲食、荷爾蒙或診斷。
3. 資料不足時直接指出還需追蹤什麼，不要用空泛保養常識填滿篇幅。
4. 建議必須對應這份紀錄，優先提供可執行的觀察或照護步驟；不可開藥、診斷疾病或承諾療效。
5. 若紀錄顯示快速惡化、明顯疼痛、滲液、腫脹、廣泛紅疹或持續不退，明確建議就醫；沒有這些證據時不要製造警訊。
6. 使用自然、親切、好懂的台灣用語。避免「紀錄集中於、全期未見、需釐清、無法判斷」等病歷或公文口吻，也不要用過多專有名詞。
7. 每一點先講結論，再補一個最重要的理由或做法；不要在同一點塞入太多部位與細節。
8. 不同問題的程度不能互相比較；「出油 2 級」後出現「其他狀況 4 級」不等於出油惡化。也不可只因不同部位出現出油與乾燥，就推定為混合肌、外油內乾或固定膚質。
9. 「觀察標籤」只能寫成「本次紀錄顯示／使用者自述／觀察到」，不可把 condition 類標籤改寫成已確診或患有某疾病。

【輸出格式】
只輸出台灣繁體中文，總長 120～200 字。固定輸出以下四個短重點，並以「｜」分隔：
最需要注意：…｜最近的變化：…｜先這樣照顧：…｜接下來觀察：…
不要輸出標題以外的前言、Markdown、分數表或免責聲明。
"""


def _zone_label(value: Any) -> str:
    try:
        return ZONE_LABELS[Zone(value)]
    except (KeyError, TypeError, ValueError):
        return str(value or "未指定")


def _issue_label(value: Any) -> str:
    try:
        return ISSUE_LABELS[Issue(value)]
    except (KeyError, TypeError, ValueError):
        return str(value or "未指定")


def _severity_label(value: Any) -> str:
    """Convert an internal severity value to user-facing wording."""
    try:
        severity = float(value)
    except (TypeError, ValueError):
        return ""
    if severity <= 2:
        return "輕微"
    if severity <= 3:
        return "中度"
    return "嚴重"


def _fallback_professional_summary(report: SkinReport) -> str:
    active = [zone for zone in report.zones if zone.record_count > 0]
    if not active:
        return "最需要注意：目前資料還不夠，先不用急著下結論｜最近的變化：再多記幾天會更容易看出趨勢｜先這樣照顧：維持簡單、溫和的日常保養｜接下來觀察：固定時間記下部位、狀況與程度。"
    worsening = [ZONE_LABELS.get(z.zone, str(z.zone)) for z in active if z.trend == "worsening"]
    improving = [ZONE_LABELS.get(z.zone, str(z.zone)) for z in active if z.trend == "improving"]
    focus = ZONE_LABELS.get(report.worst_zone, "主要紀錄部位") if report.worst_zone else "主要紀錄部位"
    trend_text = (
        f"{ '、'.join(worsening) }程度上升，{ '、'.join(improving) }呈改善"
        if worsening and improving
        else f"{ '、'.join(worsening) }程度上升" if worsening
        else f"{ '、'.join(improving) }呈改善" if improving
        else "現有部位大致持平，暫無足夠證據判定誘因"
    )
    return (
        f"最需要注意：這幾天先把照護重點放在{focus}｜最近的變化：{trend_text}｜"
        "先這樣照顧：維持溫和清潔、保濕與防曬，先別同時更換多項產品｜"
        "接下來觀察：固定記下程度和使用產品；若持續變嚴重或明顯不舒服，建議詢問皮膚科醫師。"
    )


def generate_professional_summary(
    llm_client: Any,
    report: SkinReport,
    records: List[Dict[str, Any]],
) -> str:
    """Ask Terra to synthesize trends and actions instead of restating diary text."""
    zone_details = []
    for z in report.zones:
        if z.record_count > 0:
            z_label = ZONE_LABELS.get(z.zone, str(z.zone))
            i_label = ISSUE_LABELS.get(z.top_issue, "狀況") if z.top_issue else "狀況"
            t_label = TREND_LABELS.get(z.trend, "觀察中")
            products_str = "、".join(z.products) if z.products else "無"
            zone_details.append(
                {
                    "部位": z_label,
                    "有效紀錄數": z.record_count,
                    "主要問題": i_label,
                "平均程度": _severity_label(z.avg_severity) if z.avg_severity is not None else "未指定",
                "最高程度": _severity_label(z.max_severity) or "未指定",
                    "趨勢": t_label,
                    "曾記錄產品": products_str,
                }
            )
    worst_label = ZONE_LABELS.get(report.worst_zone, "無") if report.worst_zone else "無"
    timeline = [
        {
            "日期": str(record.get("date") or ""),
            "部位": _zone_label(record.get("zone")),
            "問題": _issue_label(record.get("issue")),
                "程度": _severity_label(record.get("severity")) or "未指定",
            "產品": list(record.get("products") or []),
            "補充描述": str(record.get("note") or "")[:160],
            "狀態": str(record.get("record_status") or "recorded"),
            "觀察標籤": [
                {
                    "名稱": tag_label(slug),
                    "語氣": "使用者自述的名稱" if catalog_by_slug()[slug].condition_flag else "外觀或感受觀察",
                }
                for slug in record.get("tag_slugs", []) if slug in catalog_by_slug()
            ],
            "細分部位": detail_location_labels(record.get("zone"), record.get("detail_zones") or []),
        }
        for record in sorted(records, key=lambda item: str(item.get("date") or ""))
    ]
    evidence = {
        "分析期間": f"{report.date_from} 至 {report.date_to}",
        "連續紀錄天數": report.consecutive_days,
        "最需注意部位": worst_label,
        "部位統計": zone_details,
        "時間序列紀錄": timeline,
    }
    prompt = SYSTEM_PROMPT.format(
        report_evidence=json.dumps(evidence, ensure_ascii=False),
    )

    try:
        raw_res = llm_client.chat([{"role": "user", "content": prompt}])
        clean = raw_res.strip()
        required = ("最需要注意：", "最近的變化：", "先這樣照顧：", "接下來觀察：")
        if not all(section in clean for section in required):
            raise ValueError("report summary omitted required sections")
        return clean
    except Exception:
        return _fallback_professional_summary(report)


# Backward-compatible name for callers outside this module during rollout.
def generate_taide_summary(llm_client: Any, report: SkinReport) -> str:
    return generate_professional_summary(llm_client, report, [])


def build_report(
    user_id: str,
    records: List[Dict[str, Any]],
    date_from: Date,
    date_to: Date,
    consecutive_days: int,
    llm_client: Optional[Any] = None,
) -> SkinReport:
    """
    根據歷史紀錄列表計算各區域的統計指標與趨勢，並生成 SkinReport 物件。
    （此版本不再計算與呈現分數）
    """
    zones: List[ZoneStats] = []

    for zone in Zone:
        # 篩選屬於該區域的紀錄
        recs = [r for r in records if r.get("zone") == zone.value]

        if not recs:
            zones.append(ZoneStats(zone=zone))
            continue

        # 1. 嚴重度統計（僅保留最高嚴重度供邏輯判斷，報告文字不顯示）
        sevs = [r["severity"] for r in recs if r.get("severity") is not None]
        avg_sev = round(sum(sevs) / len(sevs), 2) if sevs else None
        max_sev = max(sevs) if sevs else None

        # 2. 主要問題統計 (出現最多次的 Issue)
        issues = [r["issue"] for r in recs if r.get("issue")]
        top_issue = Issue(Counter(issues).most_common(1)[0][0]) if issues else None

        # 3. 動態趨勢判斷
        # Severity scales describe one concern, so a different issue appearing
        # later must not be misreported as the earlier issue getting worse.
        trend = _calculate_trend(
            recs,
            issue=top_issue.value if top_issue else None,
        )

        # 4. 保養品彙整 (去重並排序)
        products = sorted({p for r in recs for p in r.get("products", []) if p})

        zones.append(
            ZoneStats(
                zone=zone,
                record_count=len(recs),
                top_issue=top_issue,
                avg_severity=avg_sev,
                max_severity=max_sev,
                trend=trend,
                products=products,
            )
        )

    # 計算最需要注意的區域 (依嚴重度較高者判斷)
    scored_zones = [z for z in zones if z.avg_severity is not None]
    worst_zone = max(scored_zones, key=lambda z: z.avg_severity).zone if scored_zones else None
    tag_counts = Counter(
        slug for record in records for slug in record.get("tag_slugs", [])
        if slug in catalog_by_slug()
    )
    top_tags = [
        TagStats(
            slug=slug, name_zh_tw=tag_label(slug),
            kind=catalog_by_slug()[slug].kind, count=count,
        )
        for slug, count in tag_counts.most_common(5)
    ]

    # 建立 SkinReport 物件
    report = SkinReport(
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        consecutive_days=consecutive_days,
        zones=zones,
        worst_zone=worst_zone,
        top_tags=top_tags,
    )

    # The model receives the timeline plus computed statistics so it can synthesize
    # evidence instead of paraphrasing the user's latest message.
    if llm_client is not None:
        report.summary_text = generate_professional_summary(llm_client, report, records)
    else:
        report.summary_text = _fallback_professional_summary(report)

    return report


def render_report_text(report: SkinReport) -> str:
    """
    將 SkinReport 結構化資料格式化為繁體中文報表（不顯示任何分數）。
    """
    lines = [
        f"📋 膚況分析表（{report.date_from} ~ {report.date_to}，連續 {report.consecutive_days} 天）",
        "",
    ]

    # 分區成果顯示
    for z in report.zones:
        label = ZONE_LABELS.get(z.zone, str(z.zone))
        if z.record_count == 0:
            lines.append(f"◻️ {label}：這段期間沒有紀錄")
            continue

        issue_str = ISSUE_LABELS.get(z.top_issue, "膚況問題") if z.top_issue else "問題"
        trend_str = TREND_LABELS.get(z.trend, "觀察中")

        # 僅顯示：主要問題與變動趨勢（不帶任何數字分數）
        lines.append(f"🔸 {label}：{issue_str}為主，{trend_str}")

        if z.products:
            lines.append(f"  搭配保養品：{'、'.join(z.products)}")

    # 最需注意區域與 LLM 總結
    if report.worst_zone:
        lines.append("")
        lines.append(f"⚠️ 最需要注意：{ZONE_LABELS.get(report.worst_zone, str(report.worst_zone))}")

    if report.top_tags:
        rendered_tags = [
            f"{tag.name_zh_tw}{'（使用者自述）' if tag.kind == 'condition' else ''}"
            for tag in report.top_tags
        ]
        lines.append(f"🔎 本次紀錄的觀察標籤：{'、'.join(rendered_tags)}")

    if report.summary_text:
        lines.append("")
        lines.append(report.summary_text)

    lines.append("")
    lines.append("（本分析僅為紀錄趨勢整理，非醫療診斷；狀況持續或惡化建議諮詢皮膚科醫師）")

    return "\n".join(lines)


def _calculate_trend(
    recs: List[Dict[str, Any]],
    issue: Optional[str] = None,
) -> str:
    """計算特定區域首末日的嚴重度變動趨勢"""
    valid_recs = [
        r
        for r in recs
        if r.get("severity") is not None
        and r.get("date")
        and (issue is None or r.get("issue") == issue)
    ]
    if not valid_recs:
        return "stable"

    by_date = sorted(valid_recs, key=lambda r: r["date"])
    first_date, last_date = by_date[0]["date"], by_date[-1]["date"]

    if first_date == last_date:
        return "stable"

    first_sevs = [r["severity"] for r in valid_recs if r["date"] == first_date]
    last_sevs = [r["severity"] for r in valid_recs if r["date"] == last_date]

    first_avg = sum(first_sevs) / len(first_sevs)
    last_avg = sum(last_sevs) / len(last_sevs)

    if last_avg < first_avg - 0.5:
        return "improving"
    elif last_avg > first_avg + 0.5:
        return "worsening"
    else:
        return "stable"
