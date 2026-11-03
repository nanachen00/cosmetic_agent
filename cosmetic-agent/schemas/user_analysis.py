from typing import Any, Dict, List

from pydantic import BaseModel, Field


class PreferredPriceRange(BaseModel):
    """
    使用者於對話中明確表達或多次出現的偏好價格範圍。

    沒有足夠價格訊號時，欄位可為 None，
    不建議使用 0 代表沒有資料，避免被誤認為偏好免費商品。
    """

    min: float | None = None
    max: float | None = None
    avg: float | None = None


class BehaviorSummary(BaseModel):
    """
    使用者於 Blink AI 對話式藥妝平台中的行為摘要。

    此摘要主要根據：
    1. 使用者在對話中明確表達的選購需求
    2. 使用者主動詢問的資訊
    3. 商品比較與推薦條件修改行為
    4. 使用者對推薦結果的回饋

    不將導購點擊直接視為實際購買。
    """

    # ==================================================
    # 資料量與可信度依據
    # ==================================================

    total_event_count: int = Field(
        default=0,
        ge=0,
        description="納入本次分析的全部事件數量"
    )

    evidence_event_count: int = Field(
        default=0,
        ge=0,
        description="可作為消費決策偏好依據的有效事件數量"
    )

    explicit_preference_count: int = Field(
        default=0,
        ge=0,
        description="使用者明確表達價格、品牌、功效或成分偏好的次數"
    )

    # ==================================================
    # 資訊關注程度
    # 所有 rate 原則上介於 0～1
    # ==================================================

    comparison_request_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者要求比較商品的比例"
    )

    ingredient_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者主動詢問成分、成分比較或避開成分的程度"
    )

    safety_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者關注刺激性、適用性、風險與安全性的程度"
    )

    review_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者主動詢問商品評價或口碑的程度"
    )

    price_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者設定預算、詢問價格或要求更便宜商品的程度"
    )

    brand_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者指定、詢問或排斥特定品牌的程度"
    )

    effect_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者以保濕、修護、抗痘等功效作為篩選條件的程度"
    )

    # ==================================================
    # 決策過程
    # ==================================================

    recommendation_refinement_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者在推薦後修改價格、功效、品牌或成分條件的比例"
    )

    alternative_request_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者要求替代商品或其他選項的比例"
    )

    repeated_product_interest_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者重複詢問或查看相同商品的程度"
    )

    recommendation_acceptance_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者接受、喜歡或肯定推薦結果的比例"
    )

    recommendation_rejection_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者拒絕、不喜歡或要求更換推薦結果的比例"
    )

    outbound_click_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="使用者點擊前往合作藥妝通路的比例，不代表實際購買"
    )

    decision_revision_count: int = Field(
        default=0,
        ge=0,
        description="使用者在商品選擇過程中修改需求條件的總次數"
    )

    # ==================================================
    # 對話中擷取出的具體偏好
    # ==================================================

    preferred_price_range: PreferredPriceRange = Field(
        default_factory=PreferredPriceRange
    )

    preferred_effects: List[str] = Field(
        default_factory=list,
        description="使用者偏好的保養功效，例如保濕、修護、抗痘"
    )

    avoid_ingredients: List[str] = Field(
        default_factory=list,
        description="使用者明確表示希望避開的成分"
    )

    preferred_brands: List[str] = Field(
        default_factory=list,
        description="使用者主動指定或反覆偏好的品牌"
    )

    avoided_brands: List[str] = Field(
        default_factory=list,
        description="使用者明確表示不希望推薦的品牌"
    )

    preferred_categories: List[str] = Field(
        default_factory=list,
        description="使用者常查詢或偏好的商品類別"
    )

    frequent_skin_concerns: List[str] = Field(
        default_factory=list,
        description="在商品選購需求中經常提及的肌膚問題"
    )

    rejection_reasons: List[str] = Field(
        default_factory=list,
        description="使用者拒絕推薦的主要原因，例如價格、成分或品牌"
    )


class CustomerTypeProfile(BaseModel):
    """
    根據 BehaviorSummary 計算出的消費決策類型。
    """

    primary_type: str

    secondary_type: str

    scores: Dict[str, float] = Field(
        default_factory=dict
    )

    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0
    )

    evidence_count: int = Field(
        default=0,
        ge=0,
        description="本次人格判斷採用的有效行為訊號數量"
    )

    supporting_signals: List[str] = Field(
        default_factory=list,
        description="支持人格判斷的主要行為訊號"
    )


class PreferenceProfile(BaseModel):
    """
    提供商品排序、推薦說明及介面呈現使用的偏好設定。
    """

    preference_tags: List[str] = Field(
        default_factory=list
    )

    ranking_weights: Dict[str, float] = Field(
        default_factory=dict
    )

    # 不能使用 Dict[str, str]，
    # 因為 copywriting_focus 與 ui_hints 實際上都是 List[str]。
    presentation_strategy: Dict[str, Any] = Field(
        default_factory=dict
    )

    profile_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0
    )

    supporting_signals: List[str] = Field(
        default_factory=list
    )