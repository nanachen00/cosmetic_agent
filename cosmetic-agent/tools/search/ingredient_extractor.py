"""Terra-backed ingredient entity extraction."""

from __future__ import annotations

import time
from app.clients.terra_completion_client import TerraCompletionClient
from core.observability import observe_pipeline_stage
from tools.search.ingredient_alias_matcher import match_single_ingredient


_SYSTEM_PROMPT = """你是一個專業的化妝品與化學成分標籤提取器。
請仔細閱讀使用者給你的句子，找出「其中客人想詢問或查詢的護膚成分名稱」（例如：積雪草、水楊酸、維他命C等）。

🚨 執行規則：
1. 只能輸出被提取出來的「成分名稱本身」（例如：積雪草）。
2. 如果找不到任何成分，請直接輸出：None
3. 禁止輸出任何多餘的解釋、標點符號、括號或贅字（例如不要回答「提取出來的成分是：積雪草」）。

=========================================
💡 參考範例 (Few-shot Examples)：

範例 ：
使用者輸入：請問這款乳液有包含水楊酸嗎？
輸出：水楊酸
=========================================
"""


class TerraIngredientExtractor:
    """Use Terra for few-shot ingredient entity extraction."""

    MODEL = "gpt-5.6-terra"
    TIMEOUT_SECONDS = 10.0
    MAX_TOKENS = 20

    def __init__(self, client: TerraCompletionClient | None = None) -> None:
        self.client = client or TerraCompletionClient.from_env()
        # Keep the legacy public attribute for callers that inspect it.
        self.api_url = "https://api.openai.com/v1/responses"

    def extract_ingredient(self, text: str) -> str | None:
        """Extract only the ingredient name, preserving the legacy output rules."""
        started = time.perf_counter()
        local = match_single_ingredient(text)
        if local is not None:
            observe_pipeline_stage("knowledge", "ingredient_extract_local", started)
            return local
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"請從以下句子中提取出最核心的保養成分名稱：\n「{text}」",
            },
        ]

        try:
            result = self.client.complete(
                messages=messages,
                model=self.MODEL,
                temperature=0.0,
                max_tokens=self.MAX_TOKENS,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
            cleaned = (
                result.replace("「", "")
                .replace("」", "")
                .replace("'", "")
                .replace('"', "")
            )
            observe_pipeline_stage("knowledge", "ingredient_extract_terra", started)
            return None if cleaned.upper() == "NONE" else cleaned
        except Exception as exc:
            observe_pipeline_stage("knowledge", "ingredient_extract_terra", started, status="error")
            print(f"[WARNING] Terra 成分提取器呼叫失敗: {type(exc).__name__}")
            return None


# 實例化供外部 import 呼叫
terra_extractor = TerraIngredientExtractor()
TAIDEIngredientExtractor = TerraIngredientExtractor
taide_extractor = terra_extractor
