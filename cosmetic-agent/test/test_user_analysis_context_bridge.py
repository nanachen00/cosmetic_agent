from tools.user.user_analysis_context_bridge import UserAnalysisContextBridge


def test_preferences_are_available_without_a_consumer_persona():
    context = UserAnalysisContextBridge.build_prompt_context(
        {
            "preferred_categories": ["乳液"],
            "preferred_effects": ["保濕", "舒敏"],
            "avoid_ingredients": ["香料", "酒精"],
            "preferred_textures": ["清爽"],
            "budget_max": 1000,
        }
    )

    assert "偏好品類：乳液" in context
    assert "避開成分：香料、酒精" in context
    assert "偏好質地：清爽" in context
    assert "常用預算：NT$1000 以內" in context
