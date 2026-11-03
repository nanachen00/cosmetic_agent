"""
Clarification Tool
對應 Root Agent 文件 Tool (9) clarification_tool
職責：當 ambiguity_score > 0.7 時，產出追問問題與快速回覆選項
追問優先順序：膚質 → 訴求 → 品類
"""
import os
from typing import List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# 追問閾值
AMBIGUITY_THRESHOLD = 0.7


class QuickReply(BaseModel):
    """快速回覆按鈕"""
    label: str = Field(description="按鈕顯示文字")
    value: str = Field(description="選擇後傳回的值")


class ClarificationResult(BaseModel):
    """clarification_tool 輸出"""
    should_clarify: bool = Field(description="是否需要追問")
    question_text: str = Field(default="", description="追問問題文字")
    quick_replies: List[QuickReply] = Field(
        default_factory=list,
        description="快速回覆按鈕選項"
    )
    clarify_dimension: str = Field(
        default="",
        description="這次追問的維度：skin_type / concern / product_category"
    )
    skip_flag: bool = Field(
        default=False,
        description="是否允許跳過追問"
    )
    fallback_message: str = Field(
        default="",
        description="使用者跳過時的回覆"
    )


# 快速回覆選項庫
SKIN_TYPE_REPLIES = [
    QuickReply(label="😓 油性肌", value="oily"),
    QuickReply(label="🌵 乾性肌", value="dry"),
    QuickReply(label="🌗 混合肌", value="combination"),
    QuickReply(label="🌸 敏感肌", value="sensitive"),
    QuickReply(label="✨ 中性肌", value="normal"),
    QuickReply(label="⏭️ 跳過", value="skip"),
]

CONCERN_REPLIES = [
    QuickReply(label="💧 保濕補水", value="保濕"),
    QuickReply(label="✨ 美白亮白", value="亮白"),
    QuickReply(label="🎯 抗痘控油", value="抗痘"),
    QuickReply(label="⏳ 抗皺逆齡", value="抗皺"),
    QuickReply(label="🌿 舒緩舒敏", value="舒敏"),
    QuickReply(label="🔬 緊緻毛孔", value="緊緻"),
    QuickReply(label="💡 透亮感", value="透亮"),
    QuickReply(label="⏭️ 跳過", value="skip"),
]

CATEGORY_REPLIES = [
    QuickReply(label="🧴 洗面乳", value="洗面乳"),
    QuickReply(label="💦 化妝水", value="化妝水"),
    QuickReply(label="💎 精華液", value="精華液"),
    QuickReply(label="🥛 乳液/乳霜", value="乳液"),
    QuickReply(label="🎭 面膜", value="面膜"),
    QuickReply(label="☀️ 防曬乳", value="防曬乳"),
    QuickReply(label="🌿 保濕棉片", value="保濕棉片"),
    QuickReply(label="⏭️ 跳過", value="skip"),
]


def get_clarification(
    ambiguity_score: float,
    skin_type: str = "unknown",
    concern: list = None,
    product_category: str = "不明",
    raw_query: str = "",
) -> ClarificationResult:
    """
    根據 intent_parser 的結果決定要追問哪個維度

    追問優先順序：
    1. 膚質不明 → 追問膚質
    2. 訴求為空 → 追問訴求
    3. 品類不明 → 追問品類
    """
    concern = concern or []

    # 不需要追問
    if ambiguity_score <= AMBIGUITY_THRESHOLD:
        return ClarificationResult(should_clarify=False)

    # 優先追問膚質
    if skin_type == "unknown":
        return ClarificationResult(
            should_clarify=True,
            clarify_dimension="skin_type",
            question_text="請問您是什麼膚質呢？選擇後我可以幫您推薦最適合的保養品！",
            quick_replies=SKIN_TYPE_REPLIES,
            skip_flag=True,
            fallback_message="沒關係！我幫您推薦適合各種膚質的熱銷商品～"
        )

    # 第二追問訴求
    if not concern:
        return ClarificationResult(
            should_clarify=True,
            clarify_dimension="concern",
            question_text="您最想改善哪個肌膚問題呢？",
            quick_replies=CONCERN_REPLIES,
            skip_flag=True,
            fallback_message="沒關係！我幫您推薦近期最受歡迎的保養品～"
        )

    # 第三追問品類
    if product_category == "不明":
        return ClarificationResult(
            should_clarify=True,
            clarify_dimension="product_category",
            question_text="您想找哪種類型的保養品呢？",
            quick_replies=CATEGORY_REPLIES,
            skip_flag=True,
            fallback_message="沒關係！我幫您推薦完整保養系列商品～"
        )

    # 都有資訊但 ambiguity 還是高（不常見），直接不追問
    return ClarificationResult(should_clarify=False)


def handle_clarification_reply(
    reply_value: str,
    clarify_dimension: str,
    original_intent: dict,
) -> dict:
    """
    處理使用者的快速回覆，更新 intent 資料

    Args:
        reply_value: 使用者選擇的值（或 'skip'）
        clarify_dimension: 這次追問的維度
        original_intent: 原始 intent_parser 結果

    Returns:
        更新後的 intent dict
    """
    updated = original_intent.copy()

    if reply_value == "skip":
        # 使用者跳過，維持原值不變
        return updated

    if clarify_dimension == "skin_type":
        updated["skin_type"] = reply_value

    elif clarify_dimension == "concern":
        # 新增到 concern 清單
        if reply_value not in updated.get("concern", []):
            updated.setdefault("concern", []).append(reply_value)

    elif clarify_dimension == "product_category":
        updated["product_category"] = reply_value

    # 更新 ambiguity_score（使用者有回答，降低模糊度）
    updated["ambiguity_score"] = max(0.0, updated.get("ambiguity_score", 1.0) - 0.4)

    return updated


if __name__ == "__main__":
    # 測試各種情境
    test_cases = [
        {
            "desc": "完全模糊（推薦東西）",
            "ambiguity_score": 0.95,
            "skin_type": "unknown",
            "concern": [],
            "product_category": "不明",
            "raw_query": "推薦東西"
        },
        {
            "desc": "有膚質但沒訴求",
            "ambiguity_score": 0.8,
            "skin_type": "oily",
            "concern": [],
            "product_category": "不明",
            "raw_query": "推薦油性肌保養品"
        },
        {
            "desc": "有膚質有訴求但沒品類",
            "ambiguity_score": 0.75,
            "skin_type": "dry",
            "concern": ["保濕"],
            "product_category": "不明",
            "raw_query": "乾肌保濕推薦"
        },
        {
            "desc": "意圖明確不需追問",
            "ambiguity_score": 0.2,
            "skin_type": "oily",
            "concern": ["抗痘"],
            "product_category": "洗面乳",
            "raw_query": "油皮痘肌洗面乳"
        },
    ]

    for case in test_cases:
        print(f"\n{'='*50}")
        print(f"情境：{case['desc']}")
        print(f"Query：{case['raw_query']}")
        print("="*50)

        result = get_clarification(
            ambiguity_score=case["ambiguity_score"],
            skin_type=case["skin_type"],
            concern=case["concern"],
            product_category=case["product_category"],
            raw_query=case["raw_query"],
        )

        print(f"需要追問：{result.should_clarify}")
        if result.should_clarify:
            print(f"追問維度：{result.clarify_dimension}")
            print(f"問題：{result.question_text}")
            print(f"按鈕選項：")
            for reply in result.quick_replies:
                print(f"  [{reply.label}]")
            print(f"可跳過：{result.skip_flag}")
            print(f"跳過回覆：{result.fallback_message}")

            # 模擬使用者選擇第一個選項
            if result.quick_replies:
                chosen = result.quick_replies[0]
                print(f"\n→ 模擬使用者選擇：{chosen.label}")
                dummy_intent = {
                    "skin_type": case["skin_type"],
                    "concern": case["concern"],
                    "product_category": case["product_category"],
                    "ambiguity_score": case["ambiguity_score"],
                }
                updated = handle_clarification_reply(
                    reply_value=chosen.value,
                    clarify_dimension=result.clarify_dimension,
                    original_intent=dummy_intent,
                )
                print(f"更新後 intent：{updated}")