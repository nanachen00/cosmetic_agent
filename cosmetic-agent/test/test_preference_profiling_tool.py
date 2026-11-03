from schemas.user_analysis import BehaviorSummary, CustomerTypeProfile

from tools.user.preference_profiling_tool import (
    generate_preference_profile
)


def main():

    behavior_summary = BehaviorSummary(
        compare_ratio=0.4,
        avg_decision_days=0.0,
        purchase_count=0,
        repeated_view_ratio=1.0,
        discount_click_rate=0.4,
        review_section_click_rate=0.8,
        ingredient_section_click_rate=0.4,
        brand_click_rate=0.0,
        new_product_view_rate=0.0,
        preferred_price_range={
            "min": 550,
            "max": 680,
            "avg": 615.0
        },
        preferred_effects=[
            "保濕",
            "舒緩",
            "修復"
        ],
        avoid_ingredients=[
            "fragrance"
        ]
    )

    customer_profile = CustomerTypeProfile(
        primary_type="review_dependent",
        secondary_type="function_oriented",
        scores={
            "hesitant_decision_maker": 0.46,
            "ingredient_researcher": 0.35,
            "price_sensitive": 0.52,
            "review_dependent": 0.74,
            "brand_oriented": 0.2,
            "explorer": 0.3,
            "function_oriented": 0.56,
            "impulse_buyer": 0.0
        },
        confidence=0.336
    )

    result = generate_preference_profile(
        behavior_summary,
        customer_profile
    )

    print("Preference Profile Result:")
    print(result)

    print("ranking_weights sum:")
    print(
        sum(
            result["ranking_weights"].values()
        )
    )


if __name__ == "__main__":
    main()