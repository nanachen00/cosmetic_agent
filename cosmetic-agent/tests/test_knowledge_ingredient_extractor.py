from __future__ import annotations

from typing import Any

from tools.search.ingredient_extractor import TAIDEIngredientExtractor


class FakeChatCompletionClient:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, *, result: str = "水楊酸", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_extract_ingredient_preserves_model_request_and_cleanup() -> None:
    client = FakeChatCompletionClient(result="「水楊酸」")
    extractor = TAIDEIngredientExtractor(client=client)

    assert extractor.extract_ingredient("請問這款乳液有包含水楊酸嗎？") == "水楊酸"
    assert extractor.api_url == "https://api.openai.com/v1/responses"
    assert len(client.calls) == 1

    call = client.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 20
    assert call["timeout_seconds"] == 10.0
    assert call["messages"][1]["content"] == (
        "請從以下句子中提取出最核心的保養成分名稱：\n"
        "「請問這款乳液有包含水楊酸嗎？」"
    )


def test_extract_ingredient_preserves_none_contract() -> None:
    client = FakeChatCompletionClient(result="None")
    extractor = TAIDEIngredientExtractor(client=client)

    assert extractor.extract_ingredient("這個適合敏感肌嗎？") is None


def test_extract_ingredient_preserves_failure_fallback() -> None:
    client = FakeChatCompletionClient(error=RuntimeError("unavailable"))
    extractor = TAIDEIngredientExtractor(client=client)

    assert extractor.extract_ingredient("水楊酸") is None
    assert len(client.calls) == 1
