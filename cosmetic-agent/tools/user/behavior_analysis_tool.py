from collections import Counter

from schemas.user_analysis import (
    BehaviorSummary,
    PreferredPriceRange
)


# =========================================================
# Event Groups
# =========================================================

EVIDENCE_EVENT_TYPES = {
    "product_search_requested",
    "preference_expressed",
    "price_constraint_expressed",
    "effect_preference_expressed",
    "brand_preference_expressed",
    "brand_avoidance_expressed",
    "ingredient_avoidance_expressed",
    "skin_concern_expressed",

    "product_information_requested",
    "ingredient_question_asked",
    "safety_question_asked",
    "review_question_asked",
    "price_question_asked",
    "brand_question_asked",
    "effect_question_asked",

    "product_comparison_requested",
    "alternative_requested",
    "recommendation_refined",
    "product_revisited",

    "recommendation_accepted",
    "recommendation_rejected",
    "recommendation_feedback",

    "outbound_product_clicked",
    "self_reported_purchase",
    "quiz_completed"
}


EXPLICIT_PREFERENCE_EVENT_TYPES = {
    "preference_expressed",
    "price_constraint_expressed",
    "effect_preference_expressed",
    "brand_preference_expressed",
    "brand_avoidance_expressed",
    "ingredient_avoidance_expressed",
    "skin_concern_expressed"
}


# =========================================================
# Helpers
# =========================================================

def safe_list(value):

    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return list(value)

    return [value]


def unique_list(values):

    result = []

    for value in values:

        if isinstance(value, str):
            value = value.strip()

        if value in ("", None):
            continue

        if value not in result:
            result.append(value)

    return result


def safe_float(
    value,
    default=None
):

    if value in (None, ""):
        return default

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    return number


def safe_non_negative_float(value):

    number = safe_float(
        value,
        default=None
    )

    if number is None or number < 0:
        return None

    return number


def calculate_rate(
    count,
    total
):

    if total <= 0:
        return 0.0

    rate = count / total

    return round(
        max(0.0, min(rate, 1.0)),
        3
    )


def should_analyze_event(event):
    """
    判斷事件是否適合用來建立這位使用者的 Profile。

    替其他人選購的需求不應成為使用者自己的偏好。
    """

    target_person = event.get(
        "target_person",
        "unknown"
    )

    return target_person != "other"


def get_event_weight(event):
    """
    根據訊號類型、事件來源、需求範圍與可信度決定權重。
    """

    signal_weights = {
        "explicit": 1.0,
        "inferred": 0.6,
        "system": 0.3
    }

    source_weights = {
        "conversation": 1.0,
        "feedback": 1.0,
        "quiz": 0.7,
        "ui": 0.6,
        "agent_trace": 0.4,
        "partner_platform": 0.8
    }

    context_weights = {
        # 使用者明確表示為長期偏好
        "long_term": 1.0,

        # 當次需求仍有參考價值，但不應直接視為永久偏好
        "session": 0.6,

        # 無法確定時給予中等權重
        "unknown": 0.8
    }

    target_weights = {
        "self": 1.0,
        "unknown": 0.8
    }

    confidence = safe_float(
        event.get("confidence", 1.0),
        default=1.0
    )

    confidence = max(
        0.0,
        min(confidence, 1.0)
    )

    weight = (
        signal_weights.get(
            event.get(
                "signal_type",
                "explicit"
            ),
            0.5
        )
        *
        source_weights.get(
            event.get(
                "event_source",
                "conversation"
            ),
            0.5
        )
        *
        context_weights.get(
            event.get(
                "context_scope",
                "unknown"
            ),
            0.8
        )
        *
        target_weights.get(
            event.get(
                "target_person",
                "unknown"
            ),
            0.0
        )
        *
        confidence
    )

    return round(
        weight,
        3
    )


def get_feedback_reason(event):

    feedback_reason = event.get(
        "feedback_reason"
    )

    if feedback_reason:
        return feedback_reason

    metadata = event.get(
        "metadata",
        {}
    )

    if isinstance(metadata, dict):
        return metadata.get(
            "feedback_reason"
        )

    return None


def add_quiz_signals(
    event,
    counters
):
    """
    將 Quiz 分數轉成低權重的冷啟動訊號。
    """

    metadata = event.get(
        "metadata",
        {}
    )

    if not isinstance(metadata, dict):
        return

    quiz_scores = metadata.get(
        "quiz_scores",
        {}
    )

    if not isinstance(quiz_scores, dict):
        return

    weight = get_event_weight(
        event
    )

    hesitant_score = safe_float(
        quiz_scores.get(
            "hesitant_decision_maker"
        ),
        0.0
    )

    ingredient_score = safe_float(
        quiz_scores.get(
            "ingredient_researcher"
        ),
        0.0
    )

    review_score = safe_float(
        quiz_scores.get(
            "review_dependent"
        ),
        0.0
    )

    price_score = safe_float(
        quiz_scores.get(
            "price_sensitive"
        ),
        0.0
    )

    brand_score = safe_float(
        quiz_scores.get(
            "brand_oriented"
        ),
        0.0
    )

    function_score = safe_float(
        quiz_scores.get(
            "function_oriented"
        ),
        0.0
    )

    counters["comparison"] += (
        hesitant_score
        * 0.5
        * weight
    )

    counters["ingredient"] += (
        ingredient_score
        * weight
    )

    counters["review"] += (
        review_score
        * weight
    )

    counters["price"] += (
        price_score
        * weight
    )

    counters["brand"] += (
        brand_score
        * weight
    )

    counters["effect"] += (
        function_score
        * weight
    )


# =========================================================
# Main Analysis
# =========================================================

def analyze_behavior(
    events
) -> BehaviorSummary:
    """
    將使用者行為事件整理成 BehaviorSummary。
    """

    if events is None:
        events = []

    if not isinstance(events, list):
        raise ValueError(
            "events 必須是 list"
        )

    counters = {
        "comparison": 0.0,
        "ingredient": 0.0,
        "safety": 0.0,
        "review": 0.0,
        "price": 0.0,
        "brand": 0.0,
        "effect": 0.0,
        "refinement": 0.0,
        "alternative": 0.0,
        "revisit": 0.0,
        "acceptance": 0.0,
        "rejection": 0.0,
        "outbound_click": 0.0
    }

    valid_event_count = 0
    evidence_event_count = 0
    explicit_preference_count = 0
    decision_revision_count = 0

    prices = []
    price_mins = []
    price_maxs = []

    preferred_effects = []
    avoid_ingredients = []
    preferred_brands = []
    avoided_brands = []
    preferred_categories = []
    frequent_skin_concerns = []
    rejection_reasons = []
    product_ids = []

    for event in events:

        if not isinstance(event, dict):
            continue

        event_type = event.get(
            "event_type"
        )

        if not event_type:
            continue

        if not should_analyze_event(event):
            continue

        weight = get_event_weight(
            event
        )

        # 權重為 0 的事件不納入分析
        if weight <= 0:
            continue

        valid_event_count += 1

        if event_type in EVIDENCE_EVENT_TYPES:
            evidence_event_count += 1

        if (
            event_type in EXPLICIT_PREFERENCE_EVENT_TYPES
            and event.get(
                "signal_type",
                "explicit"
            ) == "explicit"
        ):
            explicit_preference_count += 1

        # 商品比較
        if event_type == "product_comparison_requested":
            counters["comparison"] += weight

        # 成分關注
        if event_type in {
            "ingredient_question_asked",
            "ingredient_avoidance_expressed"
        }:
            counters["ingredient"] += weight

        if event_type == "ingredient_avoidance_expressed":
            counters["safety"] += (
                weight * 0.5
            )

        # 安全關注
        if event_type == "safety_question_asked":
            counters["safety"] += weight

        # 評價關注
        if event_type == "review_question_asked":
            counters["review"] += weight

        # 價格關注
        if event_type in {
            "price_question_asked",
            "price_constraint_expressed"
        }:
            counters["price"] += weight

        # 品牌關注
        if event_type in {
            "brand_question_asked",
            "brand_preference_expressed",
            "brand_avoidance_expressed"
        }:
            counters["brand"] += weight

        # 功效關注
        if event_type in {
            "effect_question_asked",
            "effect_preference_expressed"
        }:
            counters["effect"] += weight

        # 商品搜尋可能同時包含多種條件
        if event_type == "product_search_requested":

            if safe_list(
                event.get("preferred_effects")
            ):
                counters["effect"] += weight

            if any(
                event.get(field) is not None
                for field in [
                    "price",
                    "price_min",
                    "price_max"
                ]
            ):
                counters["price"] += weight

            if (
                safe_list(
                    event.get("preferred_brands")
                )
                or safe_list(
                    event.get("avoided_brands")
                )
            ):
                counters["brand"] += weight

            if safe_list(
                event.get("avoid_ingredients")
            ):
                counters["ingredient"] += weight
                counters["safety"] += (
                    weight * 0.5
                )

        # 修改條件
        if event_type == "recommendation_refined":
            counters["refinement"] += weight
            decision_revision_count += 1

        # 要求替代商品
        if event_type == "alternative_requested":
            counters["alternative"] += weight
            decision_revision_count += 1

        # 重複關注商品
        if event_type == "product_revisited":
            counters["revisit"] += weight

        # 接受推薦
        if event_type == "recommendation_accepted":
            counters["acceptance"] += weight

        # 拒絕推薦
        if event_type == "recommendation_rejected":
            counters["rejection"] += weight

        # 一般推薦回饋
        if event_type == "recommendation_feedback":

            feedback_type = event.get(
                "feedback_type"
            )

            if feedback_type in {
                "like",
                "accept"
            }:
                counters["acceptance"] += weight

            elif feedback_type in {
                "dislike",
                "reject"
            }:
                counters["rejection"] += weight

            elif feedback_type == "refine":
                counters["refinement"] += weight
                decision_revision_count += 1

            elif feedback_type == "request_alternative":
                counters["alternative"] += weight
                decision_revision_count += 1

        # 點擊外部商品連結，不代表購買
        if event_type == "outbound_product_clicked":
            counters["outbound_click"] += weight

        # 拒絕或修改推薦的原因
        feedback_reason = get_feedback_reason(
            event
        )

        if feedback_reason:

            rejection_reasons.append(
                feedback_reason
            )

            if feedback_reason == "too_expensive":
                counters["price"] += weight

            elif feedback_reason == "ingredient_concern":
                counters["ingredient"] += weight
                counters["safety"] += weight

            elif feedback_reason == "safety_concern":
                counters["safety"] += weight

            elif feedback_reason == "brand_not_preferred":
                counters["brand"] += weight

            elif feedback_reason == "review_not_convincing":
                counters["review"] += weight

            elif feedback_reason == "effect_not_matched":
                counters["effect"] += weight

            elif feedback_reason == "wants_alternative":
                counters["alternative"] += weight

        # Quiz 冷啟動
        if event_type == "quiz_completed":
            add_quiz_signals(
                event,
                counters
            )

        # 價格資料
        price = safe_non_negative_float(
            event.get("price")
        )

        price_min = safe_non_negative_float(
            event.get("price_min")
        )

        price_max = safe_non_negative_float(
            event.get("price_max")
        )

        if price is not None:
            prices.append(price)

        if price_min is not None:
            price_mins.append(price_min)

        if price_max is not None:
            price_maxs.append(price_max)

        # 偏好欄位
        preferred_effects.extend(
            safe_list(
                event.get(
                    "preferred_effects",
                    event.get("effects")
                )
            )
        )

        avoid_ingredients.extend(
            safe_list(
                event.get("avoid_ingredients")
            )
        )

        preferred_brands.extend(
            safe_list(
                event.get("preferred_brands")
            )
        )

        avoided_brands.extend(
            safe_list(
                event.get("avoided_brands")
            )
        )

        category = event.get(
            "category"
        )

        if category:
            preferred_categories.append(
                category
            )

        frequent_skin_concerns.extend(
            safe_list(
                event.get("skin_concerns")
            )
        )

        # 商品 ID
        event_product_ids = safe_list(
            event.get("product_ids")
        )

        product_id = event.get(
            "product_id"
        )

        if (
            product_id
            and product_id not in event_product_ids
        ):
            event_product_ids.append(
                product_id
            )

        product_ids.extend(
            unique_list(
                event_product_ids
            )
        )

    # =====================================================
    # Repeated Product Interest
    # =====================================================

    product_counts = Counter(
        product_ids
    )

    repeated_product_count = sum(
        1
        for count in product_counts.values()
        if count >= 2
    )

    repeated_from_ids = (
        repeated_product_count
        / len(product_counts)
        if product_counts
        else 0.0
    )

    denominator = max(
        evidence_event_count,
        1
    )

    repeated_product_interest_rate = max(
        calculate_rate(
            counters["revisit"],
            denominator
        ),
        round(
            repeated_from_ids,
            3
        )
    )

    # =====================================================
    # Preferred Price Range
    # =====================================================

    all_prices = (
        prices
        + price_mins
        + price_maxs
    )

    if all_prices:

        minimum_price = (
            min(price_mins)
            if price_mins
            else min(all_prices)
        )

        maximum_price = (
            max(price_maxs)
            if price_maxs
            else max(all_prices)
        )

        if minimum_price > maximum_price:
            minimum_price, maximum_price = (
                maximum_price,
                minimum_price
            )

        preferred_price_range = (
            PreferredPriceRange(
                min=minimum_price,
                max=maximum_price,
                avg=round(
                    sum(all_prices)
                    / len(all_prices),
                    2
                )
            )
        )

    else:

        preferred_price_range = (
            PreferredPriceRange(
                min=None,
                max=None,
                avg=None
            )
        )

    # =====================================================
    # Build Summary
    # =====================================================

    return BehaviorSummary(
        total_event_count=valid_event_count,

        evidence_event_count=(
            evidence_event_count
        ),

        explicit_preference_count=(
            explicit_preference_count
        ),

        comparison_request_rate=calculate_rate(
            counters["comparison"],
            denominator
        ),

        ingredient_interest_rate=calculate_rate(
            counters["ingredient"],
            denominator
        ),

        safety_interest_rate=calculate_rate(
            counters["safety"],
            denominator
        ),

        review_interest_rate=calculate_rate(
            counters["review"],
            denominator
        ),

        price_interest_rate=calculate_rate(
            counters["price"],
            denominator
        ),

        brand_interest_rate=calculate_rate(
            counters["brand"],
            denominator
        ),

        effect_interest_rate=calculate_rate(
            counters["effect"],
            denominator
        ),

        recommendation_refinement_rate=calculate_rate(
            counters["refinement"],
            denominator
        ),

        alternative_request_rate=calculate_rate(
            counters["alternative"],
            denominator
        ),

        repeated_product_interest_rate=(
            repeated_product_interest_rate
        ),

        recommendation_acceptance_rate=calculate_rate(
            counters["acceptance"],
            denominator
        ),

        recommendation_rejection_rate=calculate_rate(
            counters["rejection"],
            denominator
        ),

        outbound_click_rate=calculate_rate(
            counters["outbound_click"],
            denominator
        ),

        decision_revision_count=(
            decision_revision_count
        ),

        preferred_price_range=(
            preferred_price_range
        ),

        preferred_effects=unique_list(
            preferred_effects
        ),

        avoid_ingredients=unique_list(
            avoid_ingredients
        ),

        preferred_brands=unique_list(
            preferred_brands
        ),

        avoided_brands=unique_list(
            avoided_brands
        ),

        preferred_categories=unique_list(
            preferred_categories
        ),

        frequent_skin_concerns=unique_list(
            frequent_skin_concerns
        ),

        rejection_reasons=unique_list(
            rejection_reasons
        )
    )