"""User-facing questions and confirmations for Skin Tracking."""
from __future__ import annotations

from schemas.tracking_schema import (
    ISSUE_LABELS,
    SELECTABLE_ISSUES,
    ZONE_LABELS,
    Issue,
)

SLOT_QUESTIONS = {
    "issue": "嗨～一起看看今天的肌膚在說什麼吧 ♡ 最近有哪個變化讓你特別在意？你可以直接說完整一點，例如「這兩天下巴冒了幾顆痘，摸起來有點痛」。",
    "zone": "它主要出現在哪裡呢？你可以直接描述，例如「左眼下」、「兩頰」或「鼻翼」；也可以用下方按鈕複選。",
    "severity": "請直接用自己的話描述目前的狀況，例如範圍、數量、疼痛、持續時間，或是否影響日常；我會依你的描述判斷程度。",
    "time": "你大概從什麼時候開始注意到的？不記得確切時間也沒關係。",
}

SLOT_QUICK_REPLIES = {
    "issue": [
        {
            "label": "其他狀況…" if issue == Issue.other else ISSUE_LABELS[issue],
            "value": ISSUE_LABELS[issue],
        }
        for issue in SELECTABLE_ISSUES
    ],
    "zone": [
        {"label": label, "value": zone.value, "action": "record_location"}
        for zone, label in ZONE_LABELS.items()
    ],
    "severity": [],
    "time": [
        {"label": "今天才注意到", "value": "今天開始"},
        {"label": "大約 3 天", "value": "3天前開始"},
        {"label": "大約一週", "value": "一週前開始"},
        {"label": "一個月以上", "value": "一個月以上"},
    ],
}

OTHER_ISSUE_QUESTION = "可以呀～這種狀況可能很難用一個標籤說完。直接跟我形容你看到或感覺到什麼就好，例如刺刺的、緊繃、粗糙，或是突然長了不確定是什麼的小顆粒。"

STABILITY_CONFIRM_QUESTION = "你說的「差不多」，是指最近膚況大致維持穩定嗎？不確定也沒關係，我可以先照一般紀錄存下來。"

STABILITY_CONFIRM_QUICK_REPLIES = [
    {"label": "對，差不多維持穩定", "value": "confirm_stable"},
    {"label": "不確定，先記下來", "value": "record_only"},
]

CONFIRM_QUICK_REPLIES = [
    {"label": "好，幫我記下來", "value": "確認"},
    {"label": "我想改一下", "value": "修改"},
    {"label": "這次先不要", "value": "取消"},
]

REPORT_SUMMARY_PROMPT = """你是膚況分析助手。以下是使用者近期膚況紀錄的統計數據（JSON），請用不超過 100 字的白話文字，簡短總結整體趨勢與保養建議，不要用條列格式，不要提供醫療診斷。

統計數據：{stats_json}

請直接輸出總結文字，不要加任何前綴或解釋。"""
