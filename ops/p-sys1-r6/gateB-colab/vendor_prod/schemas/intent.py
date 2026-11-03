"""
Intent Parser 輸出 Schema
對應 Root Agent 文件 Tool (1) intent_parser
僅針對臉部保養品
"""

from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    SYMPTOM_SOLVING = "symptom_solving"
    FIND_PRODUCT = "find_product"
    RECOMMENDATION = "recommendation"
    PRICE_COMPARISON = "price_comparison"

    # 新增：保養成分、功用、使用方式等知識問題
    KNOWLEDGE_QUERY = "knowledge_query"

    UNCLEAR = "unclear"


class SkinType(str, Enum):
    OILY = "oily"
    DRY = "dry"
    COMBINATION = "combination"
    SENSITIVE = "sensitive"
    NORMAL = "normal"
    UNKNOWN = "unknown"


class ProductCategory(str, Enum):
    CLEANSER = "洗面乳"
    TONER = "化妝水"
    MASK = "面膜"
    SERUM = "精華液"
    LOTION = "乳液"
    CREAM = "乳霜"
    COTTON_PAD = "保濕棉片"
    SUNSCREEN = "防曬乳"
    ACNE_PATCH = "acne_patch"
    PORE_STRIP = "pore_strip"
    EXFOLIANT = "exfoliant"
    LIP_CARE = "lip_care"
    SPOT_CARE = "spot_care"
    EYE_CARE = "eye_care"
    MAKEUP_REMOVER = "makeup_remover"
    UNKNOWN = "不明"


class SkinConcern(str, Enum):
    HYDRATION = "保濕"
    FIRMING = "緊緻"
    ANTI_WRINKLE = "抗皺"
    BRIGHTENING = "亮白"
    RADIANCE = "透亮"
    ANTI_AGING = "逆齡"
    SOOTHING = "舒緩"
    SENSITIVE_CARE = "舒敏"
    ACNE_CARE = "抗痘"
    OIL_CONTROL = "控油"
    PORE_CARE = "毛孔"
    WHITENING = "美白"
    DARK_SPOT = "淡斑"
    UNKNOWN = "不明"


class TexturePreference(str, Enum):
    LIGHT = "清爽"
    RICH = "滋潤"
    GEL = "凝膠"
    CREAM = "乳霜"
    OIL = "油類"
    UNKNOWN = "不明"


class IntentParseResult(BaseModel):
    intent_type: IntentType = Field(
        description="使用者意圖類型"
    )

    skin_type: SkinType = Field(
        default=SkinType.UNKNOWN,
        description="使用者膚質",
    )

    concern: List[str] = Field(
        default_factory=list,
        description="肌膚困擾或訴求",
    )

    product_category: ProductCategory = Field(
        default=ProductCategory.UNKNOWN,
        description="商品品類",
    )

    texture_preference: TexturePreference = Field(
        default=TexturePreference.UNKNOWN,
        description="質地偏好",
    )

    budget_range: Optional[str] = Field(
        default=None,
        description="預算上限",
    )

    brand_preference: List[str] = Field(
        default_factory=list,
        description="品牌偏好",
    )

    urgency_flag: bool = Field(
        default=False,
        description="是否急需",
    )

    ambiguity_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="意圖模糊程度，由 function 計算",
    )

    routing_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="路由到目前意圖對應 Agent 的信心",
    )

    task_completeness: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="目前資料是否足以讓目標 Agent 開始處理",
    )

    query_expansion: List[str] = Field(
        default_factory=list,
        description="同義詞展開",
    )

    raw_query: str = Field(
        description="原始 user query"
    )


def calculate_ambiguity_score(
    result: IntentParseResult,
) -> float:
    """
    Rule-based ambiguity score 計算。

    滿分 1.0：完全不知道使用者要什麼。
    最低 0.0：意圖完全明確。
    """

    # 知識問題不需要商品類別、膚質、預算或肌膚訴求。
    # 只要已經明確判斷為 knowledge_query，就不應要求使用者補資料。
    if result.intent_type == IntentType.KNOWLEDGE_QUERY:
        if result.raw_query.strip():
            return 0.0

        return 1.0

    score = 1.0

    if result.intent_type != IntentType.UNCLEAR:
        score -= 0.20

    if result.skin_type != SkinType.UNKNOWN:
        score -= 0.25

    if len(result.concern) >= 1:
        score -= 0.20

    if len(result.concern) >= 2:
        score -= 0.05

    if result.product_category != ProductCategory.UNKNOWN:
        score -= 0.20

    if result.budget_range is not None:
        score -= 0.05

    if len(result.query_expansion) >= 3:
        score -= 0.05

    return round(
        max(0.0, min(1.0, score)),
        2,
    )

def calculate_routing_confidence(
    result: IntentParseResult,
) -> float:
    """判斷目前 intent 是否足以確定目標 Agent。"""
    if not result.raw_query.strip():
        return 0.0
    if result.intent_type == IntentType.UNCLEAR:
        return 0.0
    return 1.0


def calculate_task_completeness(
    result: IntentParseResult,
) -> float:
    """依目標 Agent 判斷目前資料是否足以開始工作。"""
    if (
        not result.raw_query.strip()
        or result.intent_type == IntentType.UNCLEAR
    ):
        return 0.0

    if result.intent_type == IntentType.KNOWLEDGE_QUERY:
        return 1.0

    if result.intent_type in (
        IntentType.FIND_PRODUCT,
        IntentType.RECOMMENDATION,
    ):
        has_product_context = (
            bool(result.concern)
            or result.product_category != ProductCategory.UNKNOWN
            or result.skin_type != SkinType.UNKNOWN
            or bool(result.brand_preference)
        )
        return 1.0 if has_product_context else 0.4

    if result.intent_type in (
        IntentType.SYMPTOM_SOLVING,
        IntentType.PRICE_COMPARISON,
    ):
        return 1.0

    return 0.5
