import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

TERRA_MODEL = os.getenv("PRODUCT_INTRO_MODEL", "gpt-5.6-terra")


def _empty_decision():
    return {
        "should_explain": False,
        "category": "",
        "intro": "",
    }


def _parse_decision(text):
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return _empty_decision()

        data = json.loads(text[start:end])
        should_explain = data.get("should_explain") is True
        intro = str(data.get("intro") or "").strip()
        category = str(data.get("category") or "").strip()

        if not should_explain or not intro:
            return _empty_decision()

        return {
            "should_explain": True,
            "category": category,
            "intro": intro,
        }
    except Exception:
        return _empty_decision()


def generate_product_intro(query, category):
    query = str(query or "").strip()
    category = str(category or "").strip()

    if not query and not category:
        return _empty_decision()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _empty_decision()

    prompt = f"""
你是 BLINK AI 的肌膚保養科普助手。

使用者原始需求：
{query}

系統解析出的產品類型提示：
{category or "未指定"}

請先判斷使用者是否需要產品類型科普。

判斷規則：
1. 提到保濕、美白、亮白、控油、防曬、抗老、痘痘、清潔等產品類型時，should_explain 為 true。
2. 只提到品牌名稱或商品名稱時，should_explain 為 false。
3. 「我想找蘭芝的保濕產品」要判斷為 true。
4. 「我想找蘭芝的產品」要判斷為 false。
5. 「我想找 SK-II」要判斷為 false。

should_explain 為 true 時：
- 只產生產品類型的核心作用、保養原理與主要方向。
- 只輸出 2～3 句繁體中文科普。
- 不要列商品。
- 不要說明如何挑選商品。
- 可列出 2～4 個與該保養方向直接相關的代表性成分，並簡述各自角色。
- 成分只使用繁體中文名稱，不補英文名稱或英文括號。
- 只描述一般配方機制，不可暗示指定商品一定含有該成分，也不可保證療效。
- 用 **重點成分**、**日間基礎** 等粗體標出最重要資訊。
- 不要加入推薦商品引導。
- 不要聲稱治療或保證效果。

範例：
美白的核心在於抑制與代謝黑色素，而亮白則著重於提升角質層含水量與促進角質代謝，以增加肌膚折射光線的能力。兩者相輔相成，有助於讓肌膚看起來更加白皙並富有透明感。

只回傳 JSON：
{{
  "should_explain": true 或 false,
  "category": "產品類型",
  "intro": "科普內容"
}}
"""

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=TERRA_MODEL,
            input=prompt,
            max_output_tokens=240,
        )
        return _parse_decision(
            str(getattr(response, "output_text", "") or "")
        )
    except Exception:
        return _empty_decision()
