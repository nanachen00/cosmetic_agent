from schemas.user_analysis import (
    BehaviorSummary,
    CustomerTypeProfile
)


# =========================================================
# Helpers
# =========================================================

def clamp_score(score):

    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0

    return round(
        max(0.0, min(score, 1.0)),
        3
    )


def normalize_count(
    value,
    maximum
):

    if maximum <= 0:
        return 0.0

    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0

    return clamp_score(
        value / maximum
    )


def has_price_preference(summary):

    price_range = (
        summary.preferred_price_range
    )

    if price_range is None:
        return 0.0

    has_price = any([
        price_range.min is not None,
        price_range.max is not None,
        price_range.avg is not None
    ])

    return 1.0 if has_price else 0.0


# =========================================================
# Confidence
# =========================================================

def calculate_confidence(
    sorted_scores,
    evidence_count,
    explicit_preference_count
):
    """
    根據人格分數差距、事件數量與明確偏好數量，
    計算本次人格判斷可信度。
    """

    if evidence_count <= 0:
        return 0.0

    if len(sorted_scores) < 2:
        return 0.0

    primary_score = sorted_scores[0][1]
    secondary_score = sorted_scores[1][1]

    if primary_score <= 0:
        return 0.0

    score_gap = max(
        primary_score - secondary_score,
        0.0
    )

    gap_score = min(
        score_gap / 0.5,
        1.0
    )

    evidence_score = min(
        max(evidence_count, 0) / 10,
        1.0
    )

    explicit_score = min(
        max(explicit_preference_count, 0) / 5,
        1.0
    )

    confidence = (
        gap_score * 0.4
        + evidence_score * 0.4
        + explicit_score * 0.2
    )

    return clamp_score(
        confidence
    )


# =========================================================
# Customer Type Scores
# =========================================================

def calculate_customer_scores(
    summary: BehaviorSummary
):

    avoid_ingredient_signal = normalize_count(
        len(summary.avoid_ingredients),
        4
    )

    preferred_effect_signal = normalize_count(
        len(summary.preferred_effects),
        4
    )

    preferred_brand_signal = normalize_count(
        len(summary.preferred_brands),
        3
    )

    avoided_brand_signal = normalize_count(
        len(summary.avoided_brands),
        3
    )

    skin_concern_signal = normalize_count(
        len(summary.frequent_skin_concerns),
        4
    )

    price_preference_signal = (
        has_price_preference(summary)
    )

    too_expensive = (
        1.0
        if "too_expensive"
        in summary.rejection_reasons
        else 0.0
    )

    review_not_convincing = (
        1.0
        if "review_not_convincing"
        in summary.rejection_reasons
        else 0.0
    )

    brand_not_preferred = (
        1.0
        if "brand_not_preferred"
        in summary.rejection_reasons
        else 0.0
    )

    effect_not_matched = (
        1.0
        if "effect_not_matched"
        in summary.rejection_reasons
        else 0.0
    )

    scores = {}

    # 猶豫決策型
    scores["hesitant_decision_maker"] = clamp_score(
        summary.comparison_request_rate * 0.25
        + summary.recommendation_refinement_rate * 0.20
        + summary.alternative_request_rate * 0.15
        + summary.repeated_product_interest_rate * 0.15
        + summary.recommendation_rejection_rate * 0.15
        + summary.review_interest_rate * 0.10
    )

    # 成分研究型
    scores["ingredient_researcher"] = clamp_score(
        summary.ingredient_interest_rate * 0.55
        + avoid_ingredient_signal * 0.25
        + summary.safety_interest_rate * 0.15
        + skin_concern_signal * 0.05
    )

    # 價格敏感型
    scores["price_sensitive"] = clamp_score(
        summary.price_interest_rate * 0.55
        + price_preference_signal * 0.20
        + too_expensive * 0.15
        + summary.alternative_request_rate * 0.10
    )

    # 評價依賴型
    scores["review_dependent"] = clamp_score(
        summary.review_interest_rate * 0.65
        + summary.comparison_request_rate * 0.15
        + review_not_convincing * 0.15
        + summary.repeated_product_interest_rate * 0.05
    )

    # 品牌導向型
    scores["brand_oriented"] = clamp_score(
        summary.brand_interest_rate * 0.55
        + preferred_brand_signal * 0.20
        + avoided_brand_signal * 0.10
        + brand_not_preferred * 0.15
    )

    # 探索型
    exploration_events = (
        summary.alternative_request_rate
        + summary.comparison_request_rate
        + summary.recommendation_refinement_rate
    )

    if exploration_events > 0:

        low_brand_dependency = max(
            0.0,
            1.0 - summary.brand_interest_rate
        )

        scores["explorer"] = clamp_score(
            summary.alternative_request_rate * 0.40
            + summary.comparison_request_rate * 0.25
            + summary.recommendation_refinement_rate * 0.20
            + low_brand_dependency * 0.15
        )

    else:

        scores["explorer"] = 0.0

    # 功效導向型
    scores["function_oriented"] = clamp_score(
        summary.effect_interest_rate * 0.50
        + preferred_effect_signal * 0.20
        + skin_concern_signal * 0.15
        + effect_not_matched * 0.15
    )

    # 快速決策傾向
    has_decision_signal = (
        summary.recommendation_acceptance_rate > 0
        or summary.outbound_click_rate > 0
    )

    if has_decision_signal:

        low_comparison = max(
            0.0,
            1.0 - summary.comparison_request_rate
        )

        low_refinement = max(
            0.0,
            1.0 - summary.recommendation_refinement_rate
        )

        low_revisit = max(
            0.0,
            1.0 - summary.repeated_product_interest_rate
        )

        scores["impulse_buyer"] = clamp_score(
            summary.recommendation_acceptance_rate * 0.40
            + summary.outbound_click_rate * 0.20
            + low_comparison * 0.15
            + low_refinement * 0.15
            + low_revisit * 0.10
        )

    else:

        scores["impulse_buyer"] = 0.0

    return scores


# =========================================================
# Supporting Signals
# =========================================================

def build_supporting_signals(
    summary,
    primary_type,
    secondary_type
):

    signals = []

    signal_rules = [
        (
            summary.comparison_request_rate,
            "使用者曾要求比較不同商品"
        ),
        (
            summary.ingredient_interest_rate,
            "使用者會主動詢問商品成分"
        ),
        (
            summary.safety_interest_rate,
            "使用者重視刺激性、適用性或成分安全"
        ),
        (
            summary.review_interest_rate,
            "使用者會主動詢問商品評價或口碑"
        ),
        (
            summary.price_interest_rate,
            "使用者會設定預算或詢問價格"
        ),
        (
            summary.brand_interest_rate,
            "使用者會指定、詢問或避開特定品牌"
        ),
        (
            summary.effect_interest_rate,
            "使用者會依照保養功效篩選商品"
        ),
        (
            summary.recommendation_refinement_rate,
            "使用者曾修改推薦條件"
        ),
        (
            summary.alternative_request_rate,
            "使用者曾要求其他商品選項"
        ),
        (
            summary.repeated_product_interest_rate,
            "使用者曾重複關注相同商品"
        ),
        (
            summary.recommendation_acceptance_rate,
            "使用者曾接受推薦結果"
        ),
        (
            summary.recommendation_rejection_rate,
            "使用者曾拒絕推薦結果"
        )
    ]

    for rate, description in signal_rules:

        if rate > 0:
            signals.append(
                description
            )

    if summary.avoid_ingredients:
        signals.append(
            "需要避開成分："
            + "、".join(
                summary.avoid_ingredients[:3]
            )
        )

    if summary.preferred_effects:
        signals.append(
            "偏好功效："
            + "、".join(
                summary.preferred_effects[:3]
            )
        )

    if summary.preferred_brands:
        signals.append(
            "偏好品牌："
            + "、".join(
                summary.preferred_brands[:3]
            )
        )

    if summary.rejection_reasons:
        signals.append(
            "拒絕或修改推薦原因："
            + "、".join(
                summary.rejection_reasons[:3]
            )
        )

    if primary_type == "unknown":

        signals.append(
            "目前有效資料不足，尚無法判斷主要決策類型"
        )

    else:

        signals.append(
            f"主要決策類型為 {primary_type}"
        )

        signals.append(
            f"次要決策類型為 {secondary_type}"
        )

    return signals[:10]


# =========================================================
# Main Profiling Function
# =========================================================

def profile_customer_type(
    summary: BehaviorSummary
) -> CustomerTypeProfile:
    """
    根據 BehaviorSummary 建立 CustomerTypeProfile。
    """

    if summary is None:
        raise ValueError(
            "summary 不可為 None"
        )

    scores = calculate_customer_scores(
        summary
    )

    sorted_scores = sorted(
        scores.items(),
        key=lambda item: item[1],
        reverse=True
    )

    has_enough_signal = (
        summary.evidence_event_count > 0
        and sorted_scores
        and sorted_scores[0][1] > 0
    )

    if has_enough_signal:

        primary_type = sorted_scores[0][0]
        secondary_type = sorted_scores[1][0]

    else:

        primary_type = "unknown"
        secondary_type = "unknown"

    confidence = calculate_confidence(
        sorted_scores=sorted_scores,
        evidence_count=(
            summary.evidence_event_count
        ),
        explicit_preference_count=(
            summary.explicit_preference_count
        )
    )

    supporting_signals = build_supporting_signals(
        summary=summary,
        primary_type=primary_type,
        secondary_type=secondary_type
    )

    return CustomerTypeProfile(
        primary_type=primary_type,
        secondary_type=secondary_type,
        scores=scores,
        confidence=confidence,
        evidence_count=(
            summary.evidence_event_count
        ),
        supporting_signals=(
            supporting_signals
        )
    )