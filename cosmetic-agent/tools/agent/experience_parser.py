"""
Product Experience Parser
=========================
使用 System Prompt 理解使用者的購買／使用經驗，輸出結構化資料。
外層 Orchestrator 仍負責對話狀態，避免 clarification 把插話誤當答案。
"""
import os

import json
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI


# 載入專案環境變數
load_dotenv()

# OpenAI 設定
# 請填入「新建立」的 API Key
# 剛才公開過的 Key 不可繼續使用
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

MODEL_NAME = "gpt-5.6-terra"

if not API_KEY:
    raise RuntimeError("缺少 OPENAI_API_KEY 環境變數")


client = OpenAI(
    api_key=API_KEY,
)


SYSTEM_PROMPT = """你是台灣藥妝保養對話中的「商品使用經驗理解器」。

請根據本輪訊息、目前等待階段、最近對話、先前推薦商品與相關長期記憶，
判斷使用者是否正在描述曾購買、使用、喜歡或不喜歡的商品。

只輸出合法 JSON：
{
  "is_experience": true,
  "product_name": "商品名稱或null",
  "sentiment": "positive、negative、neutral或unknown",
  "attributes": ["使用感受"],
  "avoid_preferences": ["未來推薦應避開的明確特徵"],
  "preferred_features": ["未來推薦應優先考慮的明確特徵"],
  "needs_product_name": false,
  "needs_experience_detail": false,
  "reply": "給使用者的繁體中文短回答"
}

規則：
1. 不可捏造商品、感受、偏好或過敏資訊。
2. 使用者只說「我買過／我用過」時，needs_product_name=true。
3. 已知商品、但沒有任何使用感受時，needs_experience_detail=true。
4. 「太黏、太油、刺激、刺痛、泛紅、過敏、沒效果、不喜歡」屬負面經驗。
5. 「清爽、好吸收、有效、喜歡、會回購」屬正面經驗。
6. 單一商品太黏，只能整理為偏好清爽／避開偏黏質地，不可推斷所有滋潤商品都有問題。
7. 如果目前等待商品名稱，使用者只輸入商品名稱，也屬於使用經驗的延續。
8. 如果目前等待使用感受，必須沿用提供的 pending_product_name，不可另猜商品。
9. reply 要自然承接對話；不可提到 GetZep、資料庫、system prompt、memory 或 context。
10. 若不是商品使用經驗，is_experience=false，其他欄位使用空值。
"""


EXPERIENCE_MARKERS = (
    "買過",
    "用過",
    "使用過",
    "正在用",
    "之前買",
    "之前用",
    "敷過",
    "擦過",
    "回購",
    "不想再買",
)


NEGATIVE_ATTRIBUTE_MAP = {
    "太黏": ("太黏", "偏黏質地"),
    "黏膩": ("黏膩", "偏黏質地"),
    "太油": ("太油", "油膩質地"),
    "油膩": ("油膩", "油膩質地"),
    "刺激": ("刺激", "刺激感"),
    "刺痛": ("刺痛", "刺激感"),
    "泛紅": ("泛紅", "容易造成泛紅的產品"),
    "過敏": ("過敏", "曾造成過敏的產品"),
    "沒效果": ("沒效果", "相同無感配方"),
    "不喜歡": ("不喜歡", ""),
}


POSITIVE_ATTRIBUTE_MAP = {
    "清爽": ("清爽", "清爽質地"),
    "不黏": ("不黏", "不黏質地"),
    "好吸收": ("好吸收", "好吸收"),
    "有效": ("有效", "曾有感的相似功效"),
    "喜歡": ("喜歡", ""),
    "會回購": ("會回購", ""),
}


def looks_like_experience(user_input: str) -> bool:
    """
    透過明確關鍵字，初步判斷使用者是否在描述商品經驗。
    """
    text = (user_input or "").strip()
    return any(marker in text for marker in EXPERIENCE_MARKERS)


def _extract_json(raw: str) -> dict:
    """
    從模型輸出中擷取 JSON。
    即使模型包了 Markdown code block，也能嘗試取出。
    """
    text = (raw or "").strip()

    if text.startswith("```"):
        text = text.split("```", 2)[1]

        if text.startswith("json"):
            text = text[4:]

    start = text.find("{")
    end = text.rfind("}") + 1

    if start < 0 or end <= start:
        raise ValueError("模型未回傳 JSON")

    return json.loads(text[start:end])


def _clean_list(value) -> list[str]:
    """
    清理模型回傳的陣列，移除空值與重複項目。
    """
    if not isinstance(value, list):
        return []

    result = []

    for item in value:
        text = str(item).strip()

        if text and text not in result:
            result.append(text)

    return result[:10]


def _fallback_parse(
    user_input: str,
    pending_product_name: str = "",
    conversation_stage: str = "",
) -> dict:
    """
    當 Terra 無法使用或模型輸出錯誤時，
    使用關鍵字規則維持基本的商品經驗判斷能力。
    """
    text = (user_input or "").strip()
    product_name = (pending_product_name or "").strip()

    is_experience = bool(product_name) or looks_like_experience(text)

    experience_prefixes = (
        "我買過",
        "我用過",
        "我使用過",
        "買過",
        "用過",
        "使用過",
        "敷過",
        "擦過",
    )

    # Orchestrator 正在等待商品名稱時，
    # 本輪訊息直接視為商品名稱。
    if conversation_stage == "waiting_product_name" and text:
        is_experience = True
        product_name = text

        for marker in experience_prefixes:
            if text.startswith(marker):
                product_name = text[len(marker):].strip(
                    " ，,。！!？?"
                )
                break

    # 尚未有商品名稱時，嘗試從「我買過 XXX」擷取商品。
    elif not product_name:
        for marker in experience_prefixes:
            if text.startswith(marker):
                candidate = text[len(marker):].strip(
                    " ，,。！!？?"
                )

                if candidate:
                    product_name = candidate

                break

    attributes = []
    avoid_preferences = []
    preferred_features = []

    for keyword, (
        attribute,
        preference,
    ) in NEGATIVE_ATTRIBUTE_MAP.items():
        if keyword in text:
            attributes.append(attribute)

            if preference:
                avoid_preferences.append(preference)

    for keyword, (
        attribute,
        preference,
    ) in POSITIVE_ATTRIBUTE_MAP.items():
        if keyword in text:
            attributes.append(attribute)

            if preference:
                preferred_features.append(preference)

    negative = any(
        keyword in text
        for keyword in NEGATIVE_ATTRIBUTE_MAP
    )

    positive = any(
        keyword in text
        for keyword in POSITIVE_ATTRIBUTE_MAP
    )

    if negative:
        sentiment = "negative"
    elif positive:
        sentiment = "positive"
    else:
        sentiment = "unknown"

    needs_product_name = (
        is_experience and not product_name
    )

    needs_experience_detail = (
        bool(product_name) and not attributes
    )

    if needs_product_name:
        reply = (
            "了解，您之前有買過相關商品。"
            "可以告訴我是哪一款嗎？"
        )

    elif needs_experience_detail:
        reply = (
            f"了解，我記下您買過「{product_name}」。"
            "使用後的感受如何呢？"
            "例如太黏、會刺激、保濕不足，"
            "或是覺得很好用。"
        )

    elif product_name and attributes:
        detail = "、".join(attributes)

        if sentiment == "negative":
            reply = (
                f"了解，您使用「{product_name}」後"
                f"覺得{detail}。"
                "之後推薦時，我會避開相同問題，"
                "優先找更符合您偏好的商品。"
            )
        else:
            reply = (
                f"了解，您使用「{product_name}」後"
                f"覺得{detail}。"
                "之後推薦時，我會優先參考這些偏好。"
            )

    else:
        reply = ""

    return {
        "is_experience": is_experience,
        "product_name": product_name,
        "sentiment": sentiment,
        "attributes": list(dict.fromkeys(attributes)),
        "avoid_preferences": list(
            dict.fromkeys(avoid_preferences)
        ),
        "preferred_features": list(
            dict.fromkeys(preferred_features)
        ),
        "needs_product_name": needs_product_name,
        "needs_experience_detail": needs_experience_detail,
        "reply": reply,
    }


def parse_product_experience(
    user_input: str,
    pending_product_name: str = "",
    conversation_stage: str = "",
    session_history: Optional[list] = None,
    memory_context: str = "",
    last_product_names: Optional[list[str]] = None,
    model: str | None = None,
) -> dict:
    """
    使用 OpenAI Terra 理解商品使用經驗。

    如果 Terra 呼叫或 JSON 解析失敗，則使用規則 fallback。
    """
    _ = model  # 保留舊參數相容性；實際語意抽取固定使用 Terra。
    fallback = _fallback_parse(
        user_input=user_input,
        pending_product_name=pending_product_name,
        conversation_stage=conversation_stage,
    )

    context_payload = {
        "conversation_stage": (
            conversation_stage or "none"
        ),
        "pending_product_name": (
            pending_product_name or None
        ),
        "last_recommended_products": (
            last_product_names or []
        )[:5],
        "recent_conversation": (
            session_history or []
        )[-6:],
        "related_memory": (
            memory_context or ""
        )[-5000:],
        "current_user_message": user_input,
    }

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": json.dumps(
                context_payload,
                ensure_ascii=False,
            ),
        },
    ]

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            reasoning_effort="none",
            max_completion_tokens=700,
            response_format={
                "type": "json_object",
            },
        )

        raw_content = response.choices[0].message.content or ""

        parsed = _extract_json(raw_content)

        result = {
            "is_experience": bool(
                parsed.get("is_experience")
            ),
            "product_name": str(
                parsed.get("product_name") or ""
            ).strip(),
            "sentiment": str(
                parsed.get("sentiment") or "unknown"
            ).strip(),
            "attributes": _clean_list(
                parsed.get("attributes")
            ),
            "avoid_preferences": _clean_list(
                parsed.get("avoid_preferences")
            ),
            "preferred_features": _clean_list(
                parsed.get("preferred_features")
            ),
            "needs_product_name": bool(
                parsed.get("needs_product_name")
            ),
            "needs_experience_detail": bool(
                parsed.get("needs_experience_detail")
            ),
            "reply": str(
                parsed.get("reply") or ""
            ).strip(),
        }

        # 等待使用感受時，不允許模型遺失既有商品名稱。
        if (
            pending_product_name
            and not result["product_name"]
        ):
            result["product_name"] = (
                pending_product_name
            )

        # 等待狀態與明確關鍵字優先，
        # 避免模型把延續訊息誤判成新需求。
        if fallback["is_experience"]:
            result["is_experience"] = True

        # 模型未找到商品名稱時，
        # 使用規則解析出的商品名稱。
        if (
            not result["product_name"]
            and fallback["product_name"]
        ):
            result["product_name"] = (
                fallback["product_name"]
            )

        # 模型沒有輸出明確資料時，
        # 才採用規則解析結果。
        if (
            not result["attributes"]
            and fallback["attributes"]
        ):
            result["attributes"] = (
                fallback["attributes"]
            )

        if (
            not result["avoid_preferences"]
            and fallback["avoid_preferences"]
        ):
            result["avoid_preferences"] = (
                fallback["avoid_preferences"]
            )

        if (
            not result["preferred_features"]
            and fallback["preferred_features"]
        ):
            result["preferred_features"] = (
                fallback["preferred_features"]
            )

        valid_sentiments = (
            "positive",
            "negative",
            "neutral",
            "unknown",
        )

        if result["sentiment"] not in valid_sentiments:
            result["sentiment"] = (
                fallback["sentiment"]
            )

        if (
            result["sentiment"] == "unknown"
            and fallback["sentiment"] != "unknown"
        ):
            result["sentiment"] = (
                fallback["sentiment"]
            )

        # 使用者只說「我買過」時，
        # 不允許模型自行捏造商品名稱。
        if (user_input or "").strip() in (
            "我買過",
            "我用過",
            "買過",
            "用過",
            "使用過",
        ):
            result["product_name"] = ""

        # 最終狀態由整理後的資料重新計算，
        # 避免模型輸出互相矛盾。
        result["needs_product_name"] = (
            result["is_experience"]
            and not result["product_name"]
        )

        result["needs_experience_detail"] = (
            bool(result["product_name"])
            and not result["attributes"]
        )

        # 如果模型沒有提供回答，
        # 使用規則產生的回答。
        if not result["reply"]:
            result["reply"] = fallback["reply"]

        return result

    except Exception as error:
        print(
            "[ExperienceParser] "
            "AI 解析失敗，改用規則 fallback："
            f"{error}"
        )

        return fallback