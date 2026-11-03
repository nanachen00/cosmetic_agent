"""
Intent Parser
=============

職責：
1. 解析使用者本輪自然語言
2. 輸出結構化意圖 IntentParseResult
3. 固定使用 OpenAI GPT-5.6 Terra
4. Terra 失敗時安全回傳 unclear，不切換其他模型
5. 避免把成分知識問題誤判成商品推薦或肌膚困擾
6. ambiguity_score 由 calculate_ambiguity_score 計算
"""

import json
import os
import re
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from schemas.intent import (
    IntentParseResult,
    IntentType,
    SkinType,
    ProductCategory,
    TexturePreference,
    calculate_ambiguity_score,
    calculate_routing_confidence,
    calculate_task_completeness,
)


load_dotenv()


# ============================================================
# OpenAI 設定
# ============================================================

# 請把新建立的 API Key 貼在這裡
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# 固定使用 Terra
OPENAI_MODEL = os.getenv("OPENAI_INTENT_MODEL", "gpt-5.6-terra").strip()

# 全域固定使用 none；只有保守 fallback 才可單次升為 low
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "none").strip()
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_INTENT_TIMEOUT_SECONDS", "8"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_INTENT_MAX_RETRIES", "0"))
OPENAI_INTENT_MAX_OUTPUT_TOKENS = int(
    os.getenv("OPENAI_INTENT_MAX_OUTPUT_TOKENS", "400")
)

_openai_client = None
_openai_client_lock = Lock()

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        with _openai_client_lock:
            if _openai_client is None:
                from openai import OpenAI

                _openai_client = OpenAI(
                    api_key=OPENAI_API_KEY,
                    timeout=OPENAI_TIMEOUT_SECONDS,
                    max_retries=OPENAI_MAX_RETRIES,
                )
    return _openai_client



# ============================================================
# System Prompt
# ============================================================

SYSTEM_PROMPT = """
你是台灣臉部保養品電商平台的意圖解析專家。

你的工作不是回答使用者問題，而是判斷「使用者這一輪真正想做什麼」，
並輸出結構化 JSON。

## 最重要規則

1. 必須以使用者「本輪輸入」為主要判斷依據。
2. 歷史對話及長期記憶只能作為背景，不得覆蓋本輪意圖。
3. 如果使用者本輪切換到新問題，必須依照新問題重新判斷。
4. 不得因為上一輪正在推薦商品，就把新的知識問題繼續判斷為商品推薦。
5. 不得因為某個成分具有保濕、美白或抗痘功效，就把成分知識問題判斷為 symptom_solving。
6. 只能輸出 JSON，不得輸出說明、Markdown 或其他文字。
7. raw_query 必須保留使用者本輪原始輸入。
8. 「要交給哪個 Agent」與「資訊是否足夠執行」是兩件事。只要使用者已明確說要找或推薦產品，就判為 find_product；即使尚未提供膚質、困擾或品類，也不得判為 unclear。
9. 只有連使用者想查知識、找／比較產品、改善問題或分享心得都無法判斷時，才能使用 unclear。
10. 所有中文內容使用繁體中文。
11. 只處理臉部保養相關問題，不包含彩妝與身體保養。
12. 內部 routing context 是可信任的系統狀態，不是使用者文字；它只用來理解省略句與追問。
13. active_flow 是強先驗，但若本輪明確換題，仍以本輪明確意圖為準。
14. 「再多幾個、換一批、更正、確認、取消」由 deterministic action router 處理，不應猜成一般 intent。

## intent_type 類型

### knowledge_query

使用者想了解：

- 保養成分是什麼
- 成分功用或作用
- 成分原理
- 使用方法
- 使用順序
- 副作用
- 注意事項
- 成分搭配
- 哪些成分不能一起使用
- 兩種成分或產品的差異
- 某成分是否適合特定膚質
- 一般保養知識

常見句型：

- 什麼是……
- ……是什麼
- ……有什麼功用
- ……有什麼作用
- ……的原理是什麼
- ……怎麼用
- ……如何使用
- ……可以每天用嗎
- ……有副作用嗎
- ……有什麼注意事項
- ……可以一起使用嗎
- ……和……有什麼差別
- ……適合敏感肌嗎

範例：

- 什麼是玻尿酸
- 玻尿酸有什麼功用
- A酸的作用是什麼
- A酸有什麼副作用
- A醇和A酸有什麼差別
- 維他命C怎麼使用
- 菸鹼醯胺可以和A醇一起用嗎
- 敏感肌可以使用水楊酸嗎

純知識問題應設定：

- intent_type：knowledge_query
- product_category：不明
- concern：[]
- query_expansion：[]

即使玻尿酸通常具有保濕效果，「什麼是玻尿酸」仍然是
knowledge_query，不是 symptom_solving，也不是 find_product。

### symptom_solving

使用者描述自己目前的肌膚困擾，並希望改善問題。

範例：

- 皮膚好乾
- 最近一直長痘
- 臉很容易出油
- 最近臉頰泛紅
- 毛孔看起來很明顯
- 最近皮膚暗沉
- 我的臉脫皮怎麼辦

只有使用者正在描述自己的肌膚問題時，才能判斷為
symptom_solving。

「什麼是玻尿酸」並不是使用者的肌膚困擾，不得判斷為
symptom_solving。

### find_product

使用者明確表示想找、想買或希望推薦某個商品。

常見句型：

- 推薦……
- 想買……
- 想找……
- 幫我找……
- 有沒有……產品
- 哪一款適合我

範例：

- 推薦含玻尿酸的精華液
- 我想買面膜
- 幫我找適合油肌的洗面乳
- 有沒有平價的美白精華
- 推薦敏感肌可以用的防曬乳

注意：

- 「什麼是玻尿酸」是 knowledge_query
- 「推薦含玻尿酸的精華液」才是 find_product
- 已提供特定商品名稱、只詢問該商品的成分、用法、評價、價格或安全性時，
  是 knowledge_query，不是 find_product；這類問題不應啟動商品推薦資料庫。

### recommendation

使用者想取得個人化保養建議、保養程序或使用順序，
但沒有明確要求購買特定商品。

範例：

- 給我保養建議
- 適合我的保養方式
- 早晚保養步驟
- 我的保養程序應該怎麼安排
- 幫我安排敏感肌保養流程

### price_comparison

使用者想比較價格、CP值或尋找平價商品。

範例：

- 哪一個比較便宜
- 有沒有CP值高的
- 幫我找平價替代品
- 這兩款哪個比較划算

### unclear

使用者輸入太短或完全無法判斷意圖。

範例：

- 推薦東西
- 隨便
- 不知道
- 都可以

## 意圖判斷優先順序

1. 詢問成分、功用、原理、差異、用法、安全性：
   knowledge_query

2. 明確要求找商品、買商品或推薦商品：
   find_product

3. 描述目前肌膚問題並希望改善：
   symptom_solving

4. 詢問個人化保養流程：
   recommendation

5. 詢問價格或CP值：
   price_comparison

6. 完全無法判斷：
   unclear

## 話題切換規則

歷史對話：

使用者：我買過未來美面膜
助理：使用後感受如何？

使用者本輪輸入：

我想問A酸功用是什麼

正確結果：

- intent_type：knowledge_query
- raw_query：我想問A酸功用是什麼

錯誤結果：

- 不得把「A酸功用」當成面膜名稱
- 不得繼續詢問面膜使用感受
- 不得判斷為 find_product
- 不得判斷為 symptom_solving

## skin_type 判斷規則

- oily：油性、油光、出油、T字出油、毛孔粗大
- dry：乾性、乾燥、脫皮、緊繃、起屑
- combination：混合肌、T字出油但兩頰乾
- sensitive：敏感肌、容易泛紅、過敏、舒敏
- normal：正常膚質、一般膚質
- unknown：本輪沒有提及膚質

純知識問題如果本輪沒有提及膚質，輸出 unknown。

## product_category 判斷規則

只能選擇以下其中一個：

- 洗面乳
- 化妝水
- 面膜
- 精華液
- 乳液
- 乳霜
- 保濕棉片
- 防曬乳
- acne_patch（痘痘貼、痘痘貼片、暗瘡貼）
- pore_strip（粉刺貼）
- exfoliant（酸類調理、去角質產品）
- lip_care（護唇膏、護唇精華、唇膜）
- spot_care（局部調理、痘痘凝膠、痘疤凝膠）
- eye_care（眼霜、眼膜、眼部精華、眼周保養）
- makeup_remover（卸妝油、卸妝水、卸妝乳、卸妝膏）
- 不明

純成分知識問題應輸出「不明」。

## concern 判斷規則

可以複選，但只能從以下選擇：

- 保濕
- 緊緻
- 抗皺
- 亮白
- 透亮
- 逆齡
- 舒緩
- 舒敏
- 抗痘
- 控油
- 毛孔
- 美白
- 淡斑

只有使用者明確描述肌膚需求或商品需求時才填入。

範例：

- 「我皮膚很乾」可以填入 ["保濕"]
- 「推薦保濕面膜」可以填入 ["保濕"]
- 「什麼是玻尿酸」必須填入 []

不得因為玻尿酸具有保濕效果，就自行填入 ["保濕"]。

## texture_preference 判斷規則

- 清爽：清爽、水感、清透、控油、不黏
- 滋潤：滋潤、乾肌、厚實、偏潤
- 凝膠：gel、凝膠、啫喱
- 乳霜：乳霜、cream、霜狀
- 油類：臉部油、護膚油
- 不明：本輪未提及

## urgency_flag 判斷規則

- true：今天要用、馬上需要、急、立刻
- false：一般詢問

## ambiguity_score

固定輸出 0.5。

程式會在取得結果後使用 calculate_ambiguity_score 重新計算。

## query_expansion 規則

只有商品搜尋、症狀改善或商品推薦類意圖需要展開。

- 抗痘 → [痘痘, 粉刺, 痤瘡, 抗痘, 控油]
- 保濕 → [保濕, 補水, 鎖水, 乾燥, 缺水]
- 美白 → [美白, 淡斑, 提亮, 均勻膚色, 暗沉]
- 抗老 → [抗老, 逆齡, 抗皺, 緊緻, 細紋]
- 舒敏 → [舒敏, 舒緩, 敏感, 低刺激, 泛紅]
- 控油 → [控油, 出油, 油光, 毛孔, T字]

knowledge_query 的 query_expansion 必須輸出空陣列。

## 輸出格式

只能輸出以下格式的純 JSON：

{
  "intent_type": "knowledge_query",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "什麼是玻尿酸"
}

## 範例一

使用者輸入：「找油皮痘肌洗面乳」

{
  "intent_type": "find_product",
  "skin_type": "oily",
  "concern": ["抗痘", "控油"],
  "product_category": "洗面乳",
  "texture_preference": "清爽",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": ["痘痘", "粉刺", "抗痘", "控油", "出油", "毛孔"],
  "raw_query": "找油皮痘肌洗面乳"
}

## 範例二

使用者輸入：「推薦東西」

{
  "intent_type": "unclear",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "推薦東西"
}

## 範例三

使用者輸入：「我皮膚很乾，預算1000內，想要保濕面膜」

{
  "intent_type": "find_product",
  "skin_type": "dry",
  "concern": ["保濕"],
  "product_category": "面膜",
  "texture_preference": "滋潤",
  "budget_range": "1000",
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": ["保濕", "補水", "鎖水", "乾燥", "缺水", "面膜"],
  "raw_query": "我皮膚很乾，預算1000內，想要保濕面膜"
}

## 範例四

使用者輸入：「什麼是玻尿酸」

{
  "intent_type": "knowledge_query",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "什麼是玻尿酸"
}

## 範例五

使用者輸入：「A酸有什麼功用」

{
  "intent_type": "knowledge_query",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "A酸有什麼功用"
}

## 範例六

使用者輸入：「A醇和A酸有什麼差別」

{
  "intent_type": "knowledge_query",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "A醇和A酸有什麼差別"
}

## 範例七

使用者輸入：「推薦含玻尿酸的保濕精華液」

{
  "intent_type": "find_product",
  "skin_type": "unknown",
  "concern": ["保濕"],
  "product_category": "精華液",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": ["玻尿酸", "保濕", "補水", "鎖水"],
  "raw_query": "推薦含玻尿酸的保濕精華液"
}

## 範例八

使用者本輪輸入：「我想問A酸功用是什麼」

{
  "intent_type": "knowledge_query",
  "skin_type": "unknown",
  "concern": [],
  "product_category": "不明",
  "texture_preference": "不明",
  "budget_range": null,
  "brand_preference": [],
  "urgency_flag": false,
  "ambiguity_score": 0.5,
  "query_expansion": [],
  "raw_query": "我想問A酸功用是什麼"
}
"""


# ============================================================
# OpenAI Structured Output Schema
# ============================================================

INTENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_type": {
            "type": "string",
            "enum": [
                "symptom_solving",
                "find_product",
                "recommendation",
                "price_comparison",
                "knowledge_query",
                "unclear",
            ],
        },
        "skin_type": {
            "type": "string",
            "enum": [
                "oily",
                "dry",
                "combination",
                "sensitive",
                "normal",
                "unknown",
            ],
        },
        "concern": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "保濕",
                    "緊緻",
                    "抗皺",
                    "亮白",
                    "透亮",
                    "逆齡",
                    "舒緩",
                    "舒敏",
                    "抗痘",
                    "控油",
                    "毛孔",
                    "美白",
                    "淡斑",
                ],
            },
        },
        "product_category": {
            "type": "string",
            "enum": [
                "洗面乳",
                "化妝水",
                "面膜",
                "精華液",
                "乳液",
                "乳霜",
                "保濕棉片",
                "防曬乳",
                "acne_patch",
                "pore_strip",
                "exfoliant",
                "lip_care",
                "spot_care",
                "eye_care",
                "makeup_remover",
                "不明",
            ],
        },
        "texture_preference": {
            "type": "string",
            "enum": [
                "清爽",
                "滋潤",
                "凝膠",
                "乳霜",
                "油類",
                "不明",
            ],
        },
        "budget_range": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ],
        },
        "brand_preference": {
            "type": "array",
            "items": {"type": "string"},
        },
        "urgency_flag": {
            "type": "boolean",
        },
        "ambiguity_score": {
            "type": "number",
        },
        "query_expansion": {
            "type": "array",
            "items": {"type": "string"},
        },
        "raw_query": {
            "type": "string",
        },
    },
    "required": [
        "intent_type",
        "skin_type",
        "concern",
        "product_category",
        "texture_preference",
        "budget_range",
        "brand_preference",
        "urgency_flag",
        "ambiguity_score",
        "query_expansion",
        "raw_query",
    ],
    "additionalProperties": False,
}


# ============================================================
# 輔助函式
# ============================================================

def _has_valid_openai_key() -> bool:
    """
    檢查程式內是否已經填入 API Key。
    """
    key = OPENAI_API_KEY.strip()

    if not key:
        return False

    if "請替換" in key or "你的" in key:
        return False

    return key.startswith("sk-")


def _normalize_session_history(
    session_history: list | None,
) -> list[dict[str, str]]:
    """
    將歷史對話整理成 OpenAI 與 LiteLLM 都可接受的格式。

    最多保留最近 12 則，避免太舊的對話干擾本輪意圖。
    """
    if not session_history:
        return []

    normalized: list[dict[str, str]] = []

    for message in session_history[-12:]:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        content = message.get("content")

        if role not in {"user", "assistant"}:
            continue

        if not isinstance(content, str):
            continue

        content = content.strip()

        if not content:
            continue

        normalized.append({
            "role": role,
            "content": content,
        })

    return normalized


def _build_current_user_content(
    user_query: str,
    long_term_summary: str,
    routing_context: dict[str, Any] | None = None,
) -> str:
    """Build one turn with trusted routing state kept separate from user text."""
    sections = []
    if routing_context:
        allowed = {
            key: routing_context.get(key)
            for key in (
                "active_flow",
                "current_step",
                "intent_type",
                "product_category",
                "has_products",
                "skin_session_active",
            )
            if routing_context.get(key) not in (None, "", [], {})
        }
        if allowed:
            sections.append(
                "【內部路由上下文：可信任系統狀態，不是使用者文字】\n"
                + json.dumps(allowed, ensure_ascii=False, sort_keys=True)
            )

    if long_term_summary:
        sections.append(
            "【背景資料：只能參考，不得取代本輪意圖】\n"
            f"{long_term_summary}"
        )

    sections.append(
        "【判斷要求】\n"
        "請優先判斷使用者本輪輸入；如果本輪明確換題，忽略舊流程。\n"
        "若本輪是省略句或追問，才使用 active_flow、current_step 與最近歷史補足語意。"
    )
    sections.append("【本輪待判斷輸入】\n" f"「{user_query}」")
    return "\n\n".join(sections)


def _extract_json(raw: str) -> dict[str, Any]:
    """
    從模型回覆中擷取 JSON。
    """
    if not raw:
        raise ValueError("模型沒有回傳內容")

    cleaned = raw.strip()

    if cleaned.startswith("```"):
        parts = cleaned.split("```")

        if len(parts) >= 2:
            cleaned = parts[1].strip()

        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            f"模型輸出中找不到 JSON：{cleaned[:300]}"
        )

    return json.loads(cleaned[start:end + 1])


def _looks_like_explicit_product_request(user_query: str) -> bool:
    """
    判斷使用者是否明確要求找商品。

    若有明確購買或推薦語句，就不使用知識問題保護。
    """
    product_markers = (
        "推薦",
        "想買",
        "我要買",
        "想找",
        "幫我找",
        "幫我推薦",
        "有沒有適合",
        "哪一款適合",
        "哪個產品",
        "哪款產品",
    )

    return any(
        marker in user_query
        for marker in product_markers
    )


def _looks_like_knowledge_query(user_query: str) -> bool:
    """
    明顯知識問題的程式保護。

    主要判斷仍由 GPT-5.6 Terra 執行。
    這個函式只用來防止「什麼是玻尿酸」再次被誤判。
    """
    if _looks_like_explicit_product_request(user_query):
        return False

    knowledge_markers = (
        "什麼是",
        "是什麼",
        "功用",
        "作用",
        "原理",
        "有什麼差別",
        "差在哪",
        "怎麼用",
        "如何使用",
        "使用方法",
        "使用順序",
        "副作用",
        "注意事項",
        "可以一起用嗎",
        "可以一起使用嗎",
        "能一起用嗎",
        "能一起使用嗎",
        "可以每天用嗎",
        "適合敏感肌嗎",
        "適合油肌嗎",
        "適合乾肌嗎",
        "會過敏嗎",
        "好用嗎",
        "評價",
        "心得",
        "成分",
        "多少錢",
        "價格",
        "哪裡買",
        "通路",
        "有新款嗎",
        "最近有出",
    )

    return any(
        marker in user_query
        for marker in knowledge_markers
    )


def _apply_knowledge_query_guard(
    parsed: dict[str, Any],
    user_query: str,
) -> dict[str, Any]:
    """
    對明顯知識問題進行最後保護。

    例如 GPT 如果把「什麼是玻尿酸」誤判為保濕困擾，
    程式會在這裡修正成 knowledge_query。
    """
    is_detail_question = _looks_like_knowledge_query(user_query)
    is_bare_product_lookup = (
        not _looks_like_explicit_product_request(user_query)
        and parsed.get("intent_type") == "find_product"
        and not parsed.get("concern")
        and parsed.get("product_category", "不明") == "不明"
    )
    if not (is_detail_question or is_bare_product_lookup):
        return parsed

    parsed["intent_type"] = "knowledge_query"
    parsed["product_category"] = "不明"
    parsed["concern"] = []
    parsed["query_expansion"] = []
    parsed["budget_range"] = None
    parsed["brand_preference"] = []
    parsed["urgency_flag"] = False

    return parsed


def _apply_obvious_product_route_guard(
    parsed: dict[str, Any],
    user_query: str,
) -> dict[str, Any]:
    """Repair only an obvious product-search route returned as unclear."""
    normalized = user_query.lower().strip()
    no_budget_markers = (
        "價格不限",
        "價錢不限",
        "預算不限",
        "不限預算",
        "沒有預算限制",
        "不設預算",
    )
    if any(marker in normalized for marker in no_budget_markers):
        parsed["budget_range"] = None

    if parsed.get("intent_type") != "unclear":
        return parsed

    search_markers = ("推薦", "找", "想買", "幫我選", "哪款適合")
    product_markers = (
        "保養",
        "產品",
        "精華",
        "乳液",
        "乳霜",
        "化妝水",
        "洗面乳",
        "防曬",
        "面膜",
        "卸妝",
        "眼霜",
        "痘痘貼",
        "局部調理",
        "痘痘凝膠",
        "痘疤凝膠",
        "眼膜",
        "眼周保養",
    )
    if any(marker in normalized for marker in search_markers) and any(
        marker in normalized for marker in product_markers
    ):
        parsed["intent_type"] = "find_product"

    return parsed

# ============================================================
# 模型呼叫
# ============================================================

def _call_openai(
    messages: list[dict[str, str]],
    reasoning_effort: str | None = None,
) -> str:
    """
    使用 OpenAI Responses API 呼叫 GPT-5.6 Terra。
    """

    # Reuse one SDK client so HTTP keep-alive and connection pooling work.


    if not _has_valid_openai_key():
        raise RuntimeError(
            "intent_parser.py 裡的 OPENAI_API_KEY 尚未設定"
        )

    client = _get_openai_client()

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=messages,
        reasoning={
            "effort": reasoning_effort or OPENAI_REASONING_EFFORT,
        },
        text={
            "format": {
                "type": "json_schema",
                "name": "intent_parse_result",
                "strict": True,
                "schema": INTENT_JSON_SCHEMA,
            },
            "verbosity": "low",
        },
        max_output_tokens=OPENAI_INTENT_MAX_OUTPUT_TOKENS,
        store=False,
    )

    raw = response.output_text

    if not raw:
        raise RuntimeError(
            f"{OPENAI_MODEL} 沒有回傳文字內容"
        )

    return raw.strip()


# ============================================================
# 對外主要函式
# ============================================================

_CONCERN_ALIASES: dict[str, str] = {
    "保濕": "保濕", "缺水": "保濕", "乾燥": "保濕",
    "緊緻": "緊緻", "彈力": "緊緻",
    "抗皺": "抗皺", "細紋": "抗皺", "皺紋": "抗皺",
    "亮白": "亮白",
    "透亮": "透亮", "暗沉": "透亮",
    "逆齡": "逆齡", "抗老": "逆齡",
    "舒緩": "舒緩", "泛紅": "舒緩",
    "舒敏": "舒敏", "敏感": "舒敏",
    "抗痘": "抗痘", "痘痘": "抗痘", "痘肌": "抗痘", "痤瘡": "抗痘",
    "粉刺": "粉刺",
    "控油": "控油", "出油": "控油",
    "毛孔": "毛孔",
    "美白": "美白",
    "淡斑": "淡斑", "色素不均": "淡斑",
    "修護": "修護", "修復": "修護", "屏障": "修護", "脫皮": "修護",
}


def _sanitize_concerns(values: Any) -> list[str]:
    """Keep only supported skincare concerns and normalize known aliases."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []

    cleaned: list[str] = []
    for value in values:
        canonical = _CONCERN_ALIASES.get(str(value).strip())
        if canonical and canonical not in cleaned:
            cleaned.append(canonical)
    return cleaned


def _parse_budget_limit(query: str) -> str | None:
    """Parse Arabic or common Chinese price limits without an LLM call."""
    match = re.search(
        r"(?:預算|價格)?\s*([0-9][0-9,]*|[零〇一二兩三四五六七八九十百千萬]+)"
        r"\s*(?:元|塊)?\s*(?:內|以內|以下|不到|不超過)",
        query,
    )
    if not match:
        return None

    raw_value = match.group(1).replace(",", "")
    if raw_value.isdigit():
        return raw_value

    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "萬": 10000}
    total = section = number = 0
    for char in raw_value:
        if char in digits:
            number = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        if unit == 10000:
            total += (section + number) * unit
            section = number = 0
        else:
            section += (number or 1) * unit
            number = 0
    value = total + section + number
    return str(value) if value > 0 else None


def _extract_explicit_brand_request(user_query: str) -> str:
    """Extract only a brand explicitly placed before 商品／產品."""
    match = re.search(
        r"(?:推薦給我|推薦|幫我找|想找|想買)\s*"
        r"([A-Za-z0-9][A-Za-z0-9 .&'_-]{0,39}|[一-鿿A-Za-z0-9 .&'_-]{2,30}?)"
        r"\s*的?(?:商品|產品)",
        (user_query or "").strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    brand = match.group(1).strip(" ，,。！？!?的")
    return "" if brand in {"我", "適合我", "適合我的", "一些", "幾個"} else brand


_PRODUCT_CATEGORY_ALIASES = (
    (ProductCategory.EXFOLIANT, ("酸類調理液", "去角質凝膠", "酵素去角質粉", "去角質泥", "去角質產品", "去角質")),
    (ProductCategory.PORE_STRIP, ("粉刺貼",)),
    (ProductCategory.ACNE_PATCH, ("痘痘貼", "痘痘貼片", "暗瘡貼", "pimple patch", "acne patch")),
    (ProductCategory.MAKEUP_REMOVER, ("卸妝水", "卸妝油", "卸妝乳", "卸妝膏", "卸妝凝膠", "卸妝慕斯", "卸妝棉", "卸妝產品", "卸妝", "makeup remover")),
    (ProductCategory.EYE_CARE, ("眼霜", "眼部精華", "眼部凝膠", "眼膜", "黑眼圈修護產品", "眼周淡紋產品", "眼周舒緩產品", "眼周保濕產品", "眼周保養", "eye cream", "眼周產品", "eye care")),
    (ProductCategory.LIP_CARE, ("護唇膏", "潤唇膏", "護唇精華", "唇膜", "唇部修護膏", "護唇產品", "護唇")),
    (ProductCategory.SUNSCREEN, ("臉部防曬乳", "臉部防曬霜", "臉部防曬凝膠", "臉部防曬噴霧", "防曬精華", "防曬棒", "防曬乳", "防曬")),
    (ProductCategory.COTTON_PAD, ("爽膚棉", "保濕棉片", "調理棉片", "棉片")),
    (ProductCategory.TONER, ("化妝水", "保濕化妝水", "舒緩化妝水", "收斂化妝水", "精華化妝水", "化妝水噴霧", "保濕噴霧", "舒緩噴霧", "前導液", "爽膚水", "機能水", "化粧水")),
    (ProductCategory.SERUM, ("精華液", "精華露", "精華水", "精華油", "安瓶", "原液", "肌底液", "保濕精華", "修護精華", "舒緩精華", "抗痘精華", "控油精華", "毛孔精華", "淡斑精華", "抗皺精華", "緊緻精華", "提亮精華", "局部精華", "刷酸精華", "精華")),
    (ProductCategory.SPOT_CARE, ("痘痘凝膠", "痘疤凝膠", "局部調理凝膠", "局部調理", "局部抗痘產品", "抗痘痘產品", "抗痘產品", "spot care")),
    (ProductCategory.LOTION, ("乳液", "清爽乳液", "保濕乳液", "修護乳液", "凝乳")),
    (ProductCategory.CREAM, ("乳霜", "保濕乳霜", "修護乳霜", "面霜", "凝霜", "水凝霜", "晚安霜", "保濕凝露", "舒緩凝膠", "局部修護霜")),
    (ProductCategory.MASK, ("片狀面膜", "保濕面膜", "修護面膜", "舒緩面膜", "抗痘面膜", "淡斑面膜", "緊緻面膜", "泥膜", "凍膜", "睡眠面膜", "泡泡面膜", "清潔面膜", "撕拉面膜", "面膜")),
    (ProductCategory.CLEANSER, ("洗面乳", "潔顏乳", "潔顏凝膠", "潔顏慕斯", "潔顏泡泡", "潔顏粉", "潔顏皂", "洗面奶", "潔面", "洗顏")),
)
_PRODUCT_CATEGORY_MATCHES = tuple(
    sorted(
        ((alias.casefold(), category) for category, aliases in _PRODUCT_CATEGORY_ALIASES for alias in aliases),
        key=lambda item: len(item[0]), reverse=True,
    )
)
_CONCERN_ONLY_PRODUCT_PHRASES = ("痘印修護產品", "粉刺調理產品", "毛孔調理產品")


def _match_product_category(query: str) -> ProductCategory:
    normalized = query.casefold()
    for alias, category in _PRODUCT_CATEGORY_MATCHES:
        if alias in normalized:
            return category
    return ProductCategory.UNKNOWN


def _fast_parse_obvious_product_query(user_query: str) -> IntentParseResult | None:
    """Keep common shopping requests off the remote intent model."""
    if os.getenv("INTENT_PRODUCT_FAST_PATH", "1").lower() not in {"1", "true", "yes", "on"}:
        return None
    query = (user_query or "").strip()
    shop_words = (
        "找", "推薦", "推建", "想買", "想找", "挑選", "選購", "預算",
        "元內", "元以內", "元以下", "塊以下", "便宜", "不超過",
    )
    if not query:
        return None
    is_bare_product_name = query.casefold() in {alias for alias, _ in _PRODUCT_CATEGORY_MATCHES}
    is_concern_only = any(phrase in query for phrase in _CONCERN_ONLY_PRODUCT_PHRASES)
    if not any(word in query for word in shop_words) and not is_bare_product_name and query not in _CONCERN_ONLY_PRODUCT_PHRASES:
        return None
    category = _match_product_category(query)
    knowledge_words = ("是什麼", "怎麼用", "如何使用", "有什麼功效", "成分是")
    strong_shop_words = ("找", "推薦", "推建", "想買", "想找", "挑選", "選購")
    if any(word in query for word in knowledge_words) and not any(word in query for word in strong_shop_words):
        return None

    budget = _parse_budget_limit(query)
    explicit_brand = _extract_explicit_brand_request(query)
    if category == ProductCategory.UNKNOWN and not explicit_brand and not is_concern_only:
        # Shopping intent is clear, but the missing category must be clarified.
        # Returning unclear here avoids a slow Terra call and prevents random
        # cross-category products from being shown merely because a budget exists.
        return IntentParseResult(
            intent_type=IntentType.UNCLEAR,
            skin_type=SkinType.UNKNOWN,
            product_category=ProductCategory.UNKNOWN,
            budget_range=budget,
            ambiguity_score=1.0,
            raw_query=query,
        )

    skin_map = (
        (("敏感肌", "敏弱肌", "敏感"), SkinType.SENSITIVE),
        (("混合肌", "混合性"), SkinType.COMBINATION),
        (("油肌", "油性肌", "油性"), SkinType.OILY),
        (("乾肌", "乾性肌", "乾性", "乾燥肌"), SkinType.DRY),
        (("中性肌", "中性"), SkinType.NORMAL),
    )
    skin_type = next(
        (value for words, value in skin_map if any(word in query for word in words)),
        SkinType.UNKNOWN,
    )
    concerns = [
        label
        for words, label in (
            (("保濕", "缺水", "乾燥"), "保濕"),
            (("敏感", "舒敏", "舒緩"), "舒緩"),
            (("粉刺", "不致粉刺"), "粉刺"),
            (("抗痘", "痘痘", "痘肌", "痤瘡"), "抗痘"),
            (("控油", "出油"), "控油"),
            (("美白", "亮白", "提亮", "淡斑", "暗沉", "膚色不均"), "美白"),
            (("修護", "修復", "屏障", "脫皮"), "修護"),
            (("抗老", "抗皺", "細紋", "緊緻", "彈力"), "逆齡"),
        )
        if any(word in query for word in words)
    ]
    terms = ([category.value] if category != ProductCategory.UNKNOWN else []) + concerns
    terms.extend(
        word
        for word in (
            "無香料", "無酒精", "不致粉刺", "清爽", "孕婦", "孕期", "懷孕",
        )
        if word in query
    )
    result = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=skin_type,
        concern=_sanitize_concerns(concerns),
        product_category=category,
        budget_range=budget,
        brand_preference=[explicit_brand] if explicit_brand else [],
        query_expansion=list(dict.fromkeys([*terms, *([explicit_brand] if explicit_brand else [])])),
        ambiguity_score=0.0,
        raw_query=query,
    )
    result.ambiguity_score = calculate_ambiguity_score(result)
    result.routing_confidence = calculate_routing_confidence(result)
    result.task_completeness = calculate_task_completeness(result)
    return result


def _apply_routing_context_guard(
    parsed: dict[str, Any],
    user_query: str,
    routing_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Use trusted product-card state only for an otherwise elliptical turn."""
    context = routing_context or {}
    flow = str(context.get("active_flow") or "").strip().lower()
    normalized = user_query.lower().strip()
    explicit_record = any(term in normalized for term in ("記錄", "紀錄", "保存", "登記"))
    explicit_knowledge = any(
        term in normalized
        for term in (
            "是什麼", "為什麼", "原因", "怎麼用", "如何使用",
            "功效", "副作用", "成分",
        )
    )
    explicit_shopping = any(
        term in normalized
        for term in (
            "推薦", "推建", "想買", "我要買", "想找", "幫我找",
            "選購", "預算", "大牌", "知名品牌", "有名的品牌",
        )
    )
    product_constraint = any(
        term in normalized
        for term in (
            "清爽", "不黏", "厚重", "便宜", "元", "預算",
            "敏感肌", "油肌", "乾肌", "混合肌",
        )
    )
    product_context = bool(
        context.get("has_products")
        or str(context.get("current_step") or "") == "product_results"
        or context.get("intent_type") in {"find_product", "recommendation", "price_comparison"}
    )
    brand_refinement = (
        any(term in normalized for term in ("品牌", "牌子", "大牌"))
        and any(
            term in normalized
            for term in (
                "比較有名", "比較知名", "更有名", "更知名",
                "知名一點", "大牌一點", "有名的品牌", "有名的牌子",
                "更多", "其他", "換", "再給我",
            )
        )
    )
    constraint_refinement = any(
        term in normalized
        for term in (
            "再便宜", "便宜一點", "清爽一點", "不要太厚重",
            "不要厚重", "不黏", "其他款", "更多款", "再給我",
            "換一款", "換一批", "換成", "改成",
        )
    )
    explicit_refinement = product_context and (brand_refinement or constraint_refinement)

    if (
        flow == "product_search"
        and not explicit_record
        and not (explicit_knowledge and not explicit_shopping and not explicit_refinement)
        and parsed.get("intent_type") not in {"find_product", "price_comparison"}
        and (explicit_shopping or product_constraint or explicit_refinement)
    ):
        parsed["intent_type"] = "find_product"

    return parsed

def _intent_result_from_raw(
    raw: str,
    user_query: str,
    routing_context: dict[str, Any] | None,
) -> IntentParseResult:
    parsed = _extract_json(raw)
    parsed["raw_query"] = user_query
    parsed["ambiguity_score"] = 0.5
    parsed = _apply_knowledge_query_guard(parsed=parsed, user_query=user_query)
    parsed = _apply_obvious_product_route_guard(parsed, user_query)
    parsed = _apply_routing_context_guard(parsed, user_query, routing_context)
    parsed["concern"] = _sanitize_concerns(parsed.get("concern"))
    result = IntentParseResult(**parsed)
    result.ambiguity_score = calculate_ambiguity_score(result)
    result.routing_confidence = calculate_routing_confidence(result)
    result.task_completeness = calculate_task_completeness(result)
    return result


def _should_try_low_fallback(
    result: IntentParseResult,
    allow_reasoning_fallback: bool,
) -> bool:
    if not allow_reasoning_fallback:
        return False
    if OPENAI_REASONING_EFFORT != "none":
        return False
    if os.getenv("INTENT_LOW_FALLBACK_ENABLED", "1").lower() not in {"1", "true", "yes", "on"}:
        return False
    threshold = float(os.getenv("INTENT_LOW_FALLBACK_THRESHOLD", "0.5"))
    return (
        result.intent_type == IntentType.UNCLEAR
        or float(result.routing_confidence or 0.0) < threshold
    )


def _prefer_low_fallback(
    primary: IntentParseResult,
    candidate: IntentParseResult,
) -> bool:
    """Accept low only for a clear improvement; never replace a stable route."""
    threshold = float(os.getenv("INTENT_LOW_FALLBACK_THRESHOLD", "0.5"))
    primary_confidence = float(primary.routing_confidence or 0.0)
    candidate_confidence = float(candidate.routing_confidence or 0.0)
    if primary.intent_type == IntentType.UNCLEAR:
        return (
            candidate.intent_type != IntentType.UNCLEAR
            and candidate_confidence >= threshold
            and candidate.ambiguity_score < primary.ambiguity_score
        )
    return (
        candidate.intent_type == primary.intent_type
        and candidate_confidence >= primary_confidence + 0.15
    )


def parse_intent(
    user_query: str,
    session_history: list | None = None,
    long_term_summary: str = "",
    model: str | None = None,
    max_retries: int = 1,
    routing_context: dict[str, Any] | None = None,
    allow_reasoning_fallback: bool = False,
) -> IntentParseResult:
    """
    解析使用者意圖。

    執行順序：
    1. 固定使用 GPT-5.6 Terra
    2. 驗證回傳 JSON
    3. 對明顯知識問題進行保護
    4. 重新計算 ambiguity_score
    5. Terra 失敗時安全回傳 unclear
    """
    _ = model  # 保留舊參數相容性；實際語意抽取固定使用 Terra。
    user_query = (user_query or "").strip()

    if not user_query:
        return IntentParseResult(
            intent_type=IntentType.UNCLEAR,
            skin_type=SkinType.UNKNOWN,
            product_category=ProductCategory.UNKNOWN,
            texture_preference=TexturePreference.UNKNOWN,
            ambiguity_score=1.0,
            raw_query="",
        )

    fast_product_intent = _fast_parse_obvious_product_query(user_query)
    if fast_product_intent is not None:
        print("[intent] obvious product fast path")
        return fast_product_intent

    history = _normalize_session_history(
        session_history
    )

    current_user_content = _build_current_user_content(
        user_query=user_query,
        long_term_summary=long_term_summary,
        routing_context=routing_context,
    )

    messages = [
        *history,
        {
            "role": "user",
            "content": current_user_content,
        },
    ]

    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            primary_effort = OPENAI_REASONING_EFFORT or "none"
            raw = _call_openai(messages, reasoning_effort=primary_effort)
            provider_name = OPENAI_MODEL
            result = _intent_result_from_raw(raw, user_query, routing_context)
            selected_effort = primary_effort

            if _should_try_low_fallback(result, allow_reasoning_fallback):
                try:
                    low_raw = _call_openai(messages, reasoning_effort="low")
                    low_result = _intent_result_from_raw(
                        low_raw,
                        user_query,
                        routing_context,
                    )
                    if _prefer_low_fallback(result, low_result):
                        result = low_result
                        selected_effort = "low_fallback"
                except Exception as fallback_error:
                    print(
                        "[intent] low fallback 失敗，保留 none 結果："
                        f"{type(fallback_error).__name__}: {fallback_error}"
                    )

            print(
                f"[intent] provider={provider_name}, "
                f"effort={selected_effort}, "
                f"intent={result.intent_type}, "
                f"query={user_query}"
            )

            return result

        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            last_error = error

            print(
                f"[intent] 第 {attempt}/{max_retries} 次解析失敗："
                f"{type(error).__name__}: {error}"
            )

        except Exception as error:
            last_error = error
            print(
                f"[intent] 第 {attempt}/{max_retries} 次 "
                f"{OPENAI_MODEL} 呼叫失敗：{error}"
            )

    print(
        f"[intent] {OPENAI_MODEL} 所有重試皆失敗，回傳 unclear。"
        f"最後錯誤：{last_error}"
    )

    return IntentParseResult(
        intent_type=IntentType.UNCLEAR,
        skin_type=SkinType.UNKNOWN,
        product_category=ProductCategory.UNKNOWN,
        texture_preference=TexturePreference.UNKNOWN,
        ambiguity_score=1.0,
        raw_query=user_query,
    )


# ============================================================
# 單獨執行測試
# ============================================================

if __name__ == "__main__":
    test_queries = [
        "什麼是玻尿酸",
        "玻尿酸有什麼功用",
        "A酸的作用是什麼",
        "A酸有什麼副作用",
        "A醇和A酸有什麼差別",
        "維他命C怎麼使用",
        "菸鹼醯胺可以跟A醇一起用嗎",
        "推薦含玻尿酸的保濕精華液",
        "我想買玻尿酸面膜",
        "我皮膚很乾，想找保濕面膜",
        "最近一直長痘痘",
        "乾肌敏感肌早晚保養程序建議",
        "哪一款比較便宜",
        "推薦東西",
        "我想問A酸功用是什麼",
    ]

    for query in test_queries:
        print("\n" + "=" * 60)
        print(f"測試輸入：{query}")
        print("=" * 60)

        test_result = parse_intent(query)

        print(
            f"intent_type：      "
            f"{test_result.intent_type}"
        )
        print(
            f"skin_type：        "
            f"{test_result.skin_type}"
        )
        print(
            f"concern：          "
            f"{test_result.concern}"
        )
        print(
            f"product_category： "
            f"{test_result.product_category}"
        )
        print(
            f"ambiguity_score：  "
            f"{test_result.ambiguity_score}"
        )
        print(
            f"query_expansion：  "
            f"{test_result.query_expansion}"
        )
