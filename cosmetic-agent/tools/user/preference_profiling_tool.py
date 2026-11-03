import json

from models.taide_client import TaideClient

from schemas.user_analysis import (
    PreferenceProfile
)


RANKING_WEIGHT_KEYS = [
    "safety_score",
    "price_value_score",
    "review_score",
    "ingredient_match_score",
    "brand_score"
]


DEFAULT_RANKING_WEIGHTS = {
    "safety_score": 0.30,
    "price_value_score": 0.20,
    "review_score": 0.15,
    "ingredient_match_score": 0.25,
    "brand_score": 0.10
}


VALID_RECOMMENDATION_STYLES = {
    "simple_direct_recommendation",
    "comparison_based",
    "review_based",
    "price_value_based",
    "ingredient_explanation_based",
    "safety_first",
    "brand_based",
    "function_based",
    "exploration_based"
}


PREFERENCE_PROMPT = """
你是 Blink AI 的使用者偏好分析模組。

請根據 behavior_summary 與 customer_type_profile，
建立可供商品分析 Agent 與推薦 Agent 使用的偏好資料。

分析重點：

1. 使用者關注價格、成分、安全、評價、品牌或功效的程度
2. 使用者主要與次要消費決策類型
3. 商品排序時各項因素的重要程度
4. 推薦內容應使用什麼呈現方式
5. UI 應優先呈現哪些資訊

ranking_weights 必須包含：

- safety_score
- price_value_score
- review_score
- ingredient_match_score
- brand_score

五個 ranking_weights 必須為非負數，總和必須等於 1.0。

recommendation_style 只能使用：

- simple_direct_recommendation
- comparison_based
- review_based
- price_value_based
- ingredient_explanation_based
- safety_first
- brand_based
- function_based
- exploration_based

請注意：

- outbound_click 只代表使用者點擊外部商品連結，不代表完成購買。
- 不可捏造輸入中不存在的行為。
- 資料不足時，偏好標籤與可信度應保持保守。
- preference_tags 最多 8 個。
- supporting_signals 最多 10 個。

請只輸出以下格式的合法 JSON，不要加入 Markdown：

{
  "preference_tags": [],
  "ranking_weights": {
    "safety_score": 0.0,
    "price_value_score": 0.0,
    "review_score": 0.0,
    "ingredient_match_score": 0.0,
    "brand_score": 0.0
  },
  "presentation_strategy": {
    "recommendation_style": "simple_direct_recommendation",
    "copywriting_focus": [],
    "ui_hints": []
  },
  "profile_confidence": 0.0,
  "supporting_signals": []
}
"""


def model_to_dict(value):
    """
    支援一般 dict、Pydantic v1 與 Pydantic v2。
    """

    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        return value.model_dump()

    if hasattr(value, "dict"):
        return value.dict()

    raise ValueError(
        "輸入必須是 dict 或 Pydantic Model"
    )


def normalize_list(value, max_items=None):
    """
    將輸入整理成不重複的字串 list。
    """

    if value is None:
        return []

    if not isinstance(value, list):
        value = [value]

    result = []

    for item in value:

        text = str(item).strip()

        if text and text not in result:
            result.append(text)

        if (
            max_items is not None
            and len(result) >= max_items
        ):
            break

    return result


def normalize_ranking_weights(weights):
    """
    確保五個排序權重存在且總和為 1。
    """

    if not isinstance(weights, dict):
        return DEFAULT_RANKING_WEIGHTS.copy()

    normalized = {}

    for key in RANKING_WEIGHT_KEYS:

        try:
            value = float(
                weights.get(key, 0)
            )
        except (TypeError, ValueError):
            value = 0.0

        normalized[key] = max(
            value,
            0.0
        )

    total = sum(
        normalized.values()
    )

    if total <= 0:
        return DEFAULT_RANKING_WEIGHTS.copy()

    normalized = {
        key: round(value / total, 4)
        for key, value in normalized.items()
    }

    difference = round(
        1.0 - sum(normalized.values()),
        4
    )

    largest_key = max(
        normalized,
        key=normalized.get
    )

    normalized[largest_key] = round(
        normalized[largest_key] + difference,
        4
    )

    return normalized


def parse_taide_json(raw_result):
    """
    解析 TAIDE 回傳的 JSON。
    """

    if isinstance(raw_result, dict):

        if "ranking_weights" in raw_result:
            return raw_result

        if "content" in raw_result:
            raw_result = raw_result["content"]

        elif "message" in raw_result:

            message = raw_result["message"]

            if isinstance(message, dict):
                raw_result = message.get(
                    "content",
                    ""
                )
            else:
                raw_result = message

        elif "choices" in raw_result:

            choices = raw_result.get(
                "choices",
                []
            )

            if choices:

                message = choices[0].get(
                    "message",
                    {}
                )

                raw_result = message.get(
                    "content",
                    ""
                )

    if not isinstance(raw_result, str):
        raise ValueError(
            "TAIDE 回傳結果不是字串或 dict"
        )

    cleaned = (
        raw_result
        .replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

    try:
        result = json.loads(
            cleaned
        )

    except json.JSONDecodeError:

        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end == -1:
            raise ValueError(
                "TAIDE 回傳內容找不到 JSON"
            )

        result = json.loads(
            cleaned[start:end + 1]
        )

    if not isinstance(result, dict):
        raise ValueError(
            "TAIDE 回傳的 JSON 必須是 object"
        )

    return result


def calculate_profile_confidence(
    behavior_summary,
    customer_profile
):
    """
    依照事件量與 Customer Type 信心值計算可信度。
    """

    evidence_count = behavior_summary.get(
        "evidence_event_count",
        0
    )

    explicit_count = behavior_summary.get(
        "explicit_preference_count",
        0
    )

    customer_confidence = customer_profile.get(
        "confidence",
        0
    )

    try:
        customer_confidence = float(
            customer_confidence
        )
    except (TypeError, ValueError):
        customer_confidence = 0.0

    confidence = (
        min(evidence_count / 10, 1.0) * 0.45
        + min(explicit_count / 5, 1.0) * 0.25
        + max(
            0.0,
            min(customer_confidence, 1.0)
        ) * 0.30
    )

    return round(
        max(0.0, min(confidence, 1.0)),
        3
    )


def validate_preference_profile(
    profile,
    behavior_summary,
    customer_profile
):
    """
    驗證 TAIDE 回傳結果。
    """

    if not isinstance(profile, dict):
        raise ValueError(
            "Preference Profile 必須是 dict"
        )

    strategy = profile.get(
        "presentation_strategy",
        {}
    )

    if not isinstance(strategy, dict):
        strategy = {}

    recommendation_style = strategy.get(
        "recommendation_style",
        "simple_direct_recommendation"
    )

    if recommendation_style not in VALID_RECOMMENDATION_STYLES:
        recommendation_style = (
            "simple_direct_recommendation"
        )

    try:
        generated_confidence = float(
            profile.get(
                "profile_confidence",
                0
            )
        )
    except (TypeError, ValueError):
        generated_confidence = 0.0

    calculated_confidence = (
        calculate_profile_confidence(
            behavior_summary,
            customer_profile
        )
    )

    profile_confidence = min(
        max(generated_confidence, 0.0),
        calculated_confidence
    )

    supporting_signals = normalize_list(
        profile.get("supporting_signals"),
        max_items=10
    )

    if not supporting_signals:
        supporting_signals = normalize_list(
            customer_profile.get(
                "supporting_signals"
            ),
            max_items=10
        )

    return {
        "preference_tags": normalize_list(
            profile.get("preference_tags"),
            max_items=8
        ),

        "ranking_weights": (
            normalize_ranking_weights(
                profile.get("ranking_weights")
            )
        ),

        "presentation_strategy": {
            "recommendation_style": (
                recommendation_style
            ),

            "copywriting_focus": normalize_list(
                strategy.get(
                    "copywriting_focus"
                ),
                max_items=8
            ),

            "ui_hints": normalize_list(
                strategy.get("ui_hints"),
                max_items=8
            )
        },

        "profile_confidence": round(
            profile_confidence,
            3
        ),

        "supporting_signals": (
            supporting_signals
        )
    }


def build_fallback_profile(
    behavior_summary,
    customer_profile
):
    """
    TAIDE 無法使用時，以規則建立基本偏好資料。
    """

    primary_type = customer_profile.get(
        "primary_type",
        ""
    )

    style_mapping = {
        "hesitant_decision_maker": (
            "comparison_based"
        ),
        "ingredient_researcher": (
            "ingredient_explanation_based"
        ),
        "price_sensitive": (
            "price_value_based"
        ),
        "review_dependent": (
            "review_based"
        ),
        "brand_oriented": (
            "brand_based"
        ),
        "explorer": (
            "exploration_based"
        ),
        "function_oriented": (
            "function_based"
        ),
        "impulse_buyer": (
            "simple_direct_recommendation"
        )
    }

    tag_mapping = {
        "hesitant_decision_maker": "重視商品比較",
        "ingredient_researcher": "重視成分資訊",
        "price_sensitive": "重視價格與CP值",
        "review_dependent": "重視商品評價",
        "brand_oriented": "重視品牌",
        "explorer": "偏好探索不同選項",
        "function_oriented": "重視商品功效",
        "impulse_buyer": "偏好快速決策"
    }

    ranking_weights = normalize_ranking_weights(
        {
            "safety_score": (
                behavior_summary.get(
                    "safety_interest_rate",
                    0
                )
                + behavior_summary.get(
                    "ingredient_interest_rate",
                    0
                ) * 0.5
            ),

            "price_value_score": (
                behavior_summary.get(
                    "price_interest_rate",
                    0
                )
            ),

            "review_score": (
                behavior_summary.get(
                    "review_interest_rate",
                    0
                )
            ),

            "ingredient_match_score": (
                behavior_summary.get(
                    "ingredient_interest_rate",
                    0
                )
                + behavior_summary.get(
                    "effect_interest_rate",
                    0
                )
            ),

            "brand_score": (
                behavior_summary.get(
                    "brand_interest_rate",
                    0
                )
            )
        }
    )

    tags = []

    if primary_type in tag_mapping:
        tags.append(
            tag_mapping[primary_type]
        )

    if behavior_summary.get(
        "avoid_ingredients"
    ):
        tags.append(
            "需要避開特定成分"
        )

    if behavior_summary.get(
        "preferred_effects"
    ):
        tags.append(
            "依功效選購"
        )

    return {
        "preference_tags": tags,

        "ranking_weights": (
            ranking_weights
        ),

        "presentation_strategy": {
            "recommendation_style": (
                style_mapping.get(
                    primary_type,
                    "simple_direct_recommendation"
                )
            ),
            "copywriting_focus": [
                "clear_reason"
            ],
            "ui_hints": [
                "show_recommendation_reason"
            ]
        },

        "profile_confidence": (
            calculate_profile_confidence(
                behavior_summary,
                customer_profile
            )
        ),

        "supporting_signals": normalize_list(
            customer_profile.get(
                "supporting_signals"
            ),
            max_items=10
        )
    }


def generate_preference_profile(
    behavior_summary,
    customer_profile
) -> PreferenceProfile:
    """
    使用 TAIDE 產生 PreferenceProfile。
    """

    if behavior_summary is None:
        raise ValueError(
            "behavior_summary 不可為 None"
        )

    if customer_profile is None:
        raise ValueError(
            "customer_profile 不可為 None"
        )

    behavior_dict = model_to_dict(
        behavior_summary
    )

    customer_dict = model_to_dict(
        customer_profile
    )

    user_content = json.dumps(
        {
            "behavior_summary": behavior_dict,
            "customer_type_profile": customer_dict
        },
        ensure_ascii=False,
        indent=2,
        default=str
    )

    try:

        client = TaideClient()

        raw_result = client.chat(
            messages=[
                {
                    "role": "system",
                    "content": PREFERENCE_PROMPT
                },
                {
                    "role": "user",
                    "content": user_content
                }
            ],
            max_tokens=900,
            temperature=0.1
        )

        parsed_result = parse_taide_json(
            raw_result
        )

        profile_data = validate_preference_profile(
            parsed_result,
            behavior_dict,
            customer_dict
        )

    except Exception:

        profile_data = build_fallback_profile(
            behavior_dict,
            customer_dict
        )

    return PreferenceProfile(
        preference_tags=profile_data[
            "preference_tags"
        ],

        ranking_weights=profile_data[
            "ranking_weights"
        ],

        presentation_strategy=profile_data[
            "presentation_strategy"
        ],

        profile_confidence=profile_data[
            "profile_confidence"
        ],

        supporting_signals=profile_data[
            "supporting_signals"
        ]
    )