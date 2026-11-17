from __future__ import annotations

from typing import Any
import pytest

from tools.search.ingredient_extractor import TAIDEIngredientExtractor
from tools.search.ingredient_alias_matcher import IngredientAliasMatcher


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

    query = "昨天擦水楊酸後刺痛，今天還能繼續用嗎？"
    assert extractor.extract_ingredient(query) == "水楊酸"
    assert extractor.api_url == "https://api.openai.com/v1/responses"
    assert len(client.calls) == 1

    call = client.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 20
    assert call["timeout_seconds"] == 10.0
    assert call["messages"][1]["content"] == (
        "請從以下句子中提取出最核心的保養成分名稱：\n"
        f"「{query}」"
    )


def test_extract_ingredient_preserves_none_contract() -> None:
    client = FakeChatCompletionClient(result="None")
    extractor = TAIDEIngredientExtractor(client=client)

    assert extractor.extract_ingredient("這個適合敏感肌嗎？") is None


def test_extract_ingredient_preserves_failure_fallback() -> None:
    client = FakeChatCompletionClient(error=RuntimeError("unavailable"))
    extractor = TAIDEIngredientExtractor(client=client)

    assert extractor.extract_ingredient("熊果素") is None
    assert len(client.calls) == 1


@pytest.mark.parametrize("query,canonical", [
    ("水楊酸", "水楊酸"),
    ("請問這款乳液有包含水楊酸嗎？", "水楊酸"),
    ("BHA 是什麼？", "水楊酸"),
    ("ＢＨＡ的功效", "水楊酸"),
    ("What is SALICYLIC ACID?", "水楊酸"),
    ("Hyaluronic Acid", "玻尿酸"),
    ("透明質酸有什麼功用", "玻尿酸"),
    ("What does RETINOL do?", "視黃醇"),
    ("A 醇是什麼", "視黃醇"),
    ("ceramide np", "神經醯胺"),
    ("神經醯胺（Ceramide）", "神經醯胺"),
    ("glycerol", "甘油"),
])
def test_single_explicit_curated_ingredient_avoids_model(query, canonical):
    client = FakeChatCompletionClient(error=AssertionError("local entity must not call model"))
    assert TAIDEIngredientExtractor(client=client).extract_ingredient(query) == canonical
    assert client.calls == []


@pytest.mark.parametrize("query", [
    "水楊酸和A醇可以一起用嗎", "水楊酸跟熊果素", "水楊酸、玻尿酸",
    "水楊酸鈉", "Retinolate", "Retinol palmitate", "polyglycerin", "BHAs",
    "acetyl salicylic acid", "孕婦可以用水楊酸嗎", "不含水楊酸的可以嗎",
    "BHA 2%", "視黃醇棕櫚酸酯", "熊果素", "保濕成分", "水楊酸(BHA)是什麼",
])
def test_uncertain_multi_ingredient_safety_and_inci_suffix_defer_to_model(query):
    client = FakeChatCompletionClient(result="None")
    assert TAIDEIngredientExtractor(client=client).extract_ingredient(query) is None
    assert len(client.calls) == 1
    assert query in client.calls[0]["messages"][1]["content"]


def test_ambiguous_alias_and_unavailable_catalog_never_guess(monkeypatch, tmp_path):
    nodes = [dict(type="Ingredient", name="甲", aliases=["shared"]),
             dict(type="Ingredient", name="乙", aliases=["shared"])]
    assert IngredientAliasMatcher(nodes).match("shared") is None
    import tools.search.ingredient_alias_matcher as module
    module._matcher.cache_clear()
    try:
        path = tmp_path / "invalid.json"
        path.write_text("not JSON")
        monkeypatch.setattr(module, "VOCABULARY", path)
        assert module.match_single_ingredient("水楊酸") is None
    finally:
        module._matcher.cache_clear()
