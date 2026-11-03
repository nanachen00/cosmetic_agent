"""Feature 0 tests for structured Skin Tracking extraction."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.skin_tracking.extraction import (
    OpenAISkinExtractor,
    SkinConcernAssessment,
    SkinExtractionConfigurationError,
    SkinExtractionProviderError,
    SkinExtractionResult,
)


class RecordingResponses:
    """Minimal OpenAI SDK boundary used to verify our request contract."""

    def __init__(self, parsed=None, error: Exception | None = None):
        self.parsed = parsed
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_parsed=self.parsed)


class RecordingOpenAIClient:
    def __init__(self, responses: RecordingResponses):
        self.responses = responses


def test_terra_request_uses_structured_outputs():
    parsed = SkinExtractionResult.model_validate(
        {
            "records": [
                {
                    "intent": "create",
                    "issue": "acne",
                    "zone": "chin",
                    "severity": 3,
                    "stability_signal": "stable_explicit",
                }
            ]
        }
    )
    responses = RecordingResponses(parsed=parsed)
    extractor = OpenAISkinExtractor(client=RecordingOpenAIClient(responses))

    result = extractor.extract("下巴長痘痘，大概 3 分", today=date(2026, 8, 16))

    assert result.model_dump() == parsed.model_copy(update={"records": [parsed.records[0].model_copy(update={"tag_slugs": [], "detail_zones": ["chin"]})]}).model_dump()
    assert len(responses.calls) == 1
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["text_format"] is SkinExtractionResult
    assert request["input"] == "下巴長痘痘，大概 3 分"
    assert "2026-08-16" in request["instructions"]
    assert request["store"] is False
    assert result.records[0].stability_signal == "stable_explicit"


def test_concern_assessment_uses_model_reasoning_and_structured_output():
    parsed = SkinConcernAssessment.model_validate({
        "concern_summary": "眼下出現像脂肪粒的小顆粒",
        "clarifying_question": "這些小顆粒是在左眼下、右眼下，還是兩邊都有？",
        "severity_levels": [
            {"level": 1, "description": "摸起來才感覺得到"},
            {"level": 2, "description": "近看有少量小顆粒"},
            {"level": 3, "description": "一眼可見或數量變多"},
            {"level": 4, "description": "範圍擴大並伴隨不適"},
            {"level": 5, "description": "快速變多、紅腫或疼痛"},
        ],
    })
    responses = RecordingResponses(parsed=parsed)
    extractor = OpenAISkinExtractor(client=RecordingOpenAIClient(responses))

    result = extractor.assess_concern(
        issue_label="其他",
        user_description="眼睛下面長一粒粒很像脂肪粒的東西",
        zone_label="左臉頰區（含左眼周）",
    )

    assert result == parsed
    request = responses.calls[0]
    assert request["text_format"] is SkinConcernAssessment
    assert "眼睛下面長一粒粒" in request["input"]
    assert "不是診斷" in request["instructions"]
    assert "不是照抄整句" in request["instructions"]
    assert request["store"] is False


def test_missing_api_key_is_a_configuration_error():
    with patch.dict(os.environ, {}, clear=True):
        try:
            OpenAISkinExtractor(env_file=None)
            raise AssertionError("missing API key must fail during configuration")
        except SkinExtractionConfigurationError as exc:
            assert "OPENAI_API_KEY" in str(exc)


def test_project_env_file_is_loaded_without_overriding_process_environment():
    with TemporaryDirectory() as directory:
        env_file = Path(directory) / ".env"
        env_file.write_text("OPENAI_API_KEY=test-only-key\n", encoding="utf-8")

        with patch("openai.OpenAI") as openai_class:
            with patch.dict(os.environ, {}, clear=True):
                OpenAISkinExtractor(env_file=env_file)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "process-key"}, clear=True):
                OpenAISkinExtractor(env_file=env_file)

        assert openai_class.call_args_list[0].kwargs["api_key"] == "test-only-key"
        assert openai_class.call_args_list[1].kwargs["api_key"] == "process-key"


def test_provider_failure_is_not_silently_treated_as_empty_input():
    responses = RecordingResponses(error=TimeoutError("provider timeout"))
    extractor = OpenAISkinExtractor(client=RecordingOpenAIClient(responses))

    try:
        extractor.extract("今天臉很乾", today=date(2026, 8, 16))
        raise AssertionError("provider failure must be visible to the caller")
    except SkinExtractionProviderError as exc:
        assert "gpt-5.6-terra" in str(exc)


def test_missing_parsed_output_is_a_provider_error():
    responses = RecordingResponses(parsed=None)
    extractor = OpenAISkinExtractor(client=RecordingOpenAIClient(responses))

    try:
        extractor.extract("今天臉很乾", today=date(2026, 8, 16))
        raise AssertionError("missing structured output must fail")
    except SkinExtractionProviderError as exc:
        assert "structured output" in str(exc).lower()


def test_invalid_parsed_output_is_a_provider_error():
    responses = RecordingResponses(parsed={"records": [{"severity": 9}]})
    extractor = OpenAISkinExtractor(client=RecordingOpenAIClient(responses))

    try:
        extractor.extract("嚴重度 9 分", today=date(2026, 8, 16))
        raise AssertionError("invalid structured output must fail validation")
    except SkinExtractionProviderError as exc:
        assert "invalid structured output" in str(exc).lower()


if __name__ == "__main__":
    test_terra_request_uses_structured_outputs()
    test_missing_api_key_is_a_configuration_error()
    test_project_env_file_is_loaded_without_overriding_process_environment()
    test_provider_failure_is_not_silently_treated_as_empty_input()
    test_missing_parsed_output_is_a_provider_error()
    test_invalid_parsed_output_is_a_provider_error()
    print("Skin extraction provider tests passed")
