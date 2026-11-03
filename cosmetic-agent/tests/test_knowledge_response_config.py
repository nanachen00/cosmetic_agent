from core.knowledge_response_config import KnowledgeResponseConfig


def test_terra_defaults_match_existing_knowledge_agent() -> None:
    config = KnowledgeResponseConfig.from_env({})

    assert config.provider == "terra"
    assert config.model == "gpt-5.6-terra"
    assert config.max_output_tokens == 384
    assert config.timeout_seconds == 45.0
    assert config.max_retries == 1
    assert config.reasoning_effort == "none"


def test_terra_reasoning_effort_is_preserved() -> None:
    config = KnowledgeResponseConfig.from_env(
        {"TERRA_RESPONSE_REASONING_EFFORT": "low"}
    )

    assert config.reasoning_effort == "low"


def test_taide_defaults_match_existing_knowledge_agent() -> None:
    config = KnowledgeResponseConfig.from_env(
        {"KNOWLEDGE_RESPONSE_PROVIDER": "taide"}
    )

    assert config.provider == "terra"
    assert config.model == "gpt-5.6-terra"
    assert config.max_output_tokens == 384
    assert config.timeout_seconds == 45.0
    assert config.max_retries == 1
    assert config.reasoning_effort == "none"
    assert config.taide_api_base == ""
    assert config.taide_api_key == "EMPTY"


def test_explicit_settings_ignore_retired_provider_transport() -> None:
    config = KnowledgeResponseConfig.from_env(
        {
            "KNOWLEDGE_RESPONSE_PROVIDER": " TAIDE ",
            "KNOWLEDGE_RESPONSE_MODEL": " custom-model ",
            "KNOWLEDGE_RESPONSE_MAX_OUTPUT_TOKENS": "512",
            "TAIDE_RESPONSE_TIMEOUT_SECONDS": "17.5",
            "TAIDE_BASE_URL": "/http://model.internal/v1/",
            "TAIDE_API_KEY": "local-key",
        }
    )

    assert config.provider == "terra"
    assert config.model == "custom-model"
    assert config.max_output_tokens == 512
    assert config.timeout_seconds == 45.0
    assert config.taide_api_base == ""
    assert config.taide_api_key == "EMPTY"


def test_blank_model_uses_terra_default() -> None:
    terra = KnowledgeResponseConfig.from_env(
        {"KNOWLEDGE_RESPONSE_MODEL": "   "}
    )
    normalized = KnowledgeResponseConfig.from_env(
        {
            "KNOWLEDGE_RESPONSE_PROVIDER": "other",
            "KNOWLEDGE_RESPONSE_MODEL": "   ",
        }
    )

    assert terra.model == "gpt-5.6-terra"
    assert normalized.model == "gpt-5.6-terra"
