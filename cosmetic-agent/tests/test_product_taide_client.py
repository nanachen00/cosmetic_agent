from __future__ import annotations

import json

from agents.product import product_agent


def test_batch_ingredient_extraction_delegates_to_shared_chat_client(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        return "結果：" + json.dumps(
            {"0": ["玻尿酸"], "1": ["神經醯胺"]},
            ensure_ascii=False,
        )

    monkeypatch.setenv("PRODUCT_INGREDIENT_EXTRACTOR", "terra")
    monkeypatch.setattr(product_agent._terra_client, "complete", fake_complete)

    result = product_agent._extract_ingredients_batch_taide(
        [
            {"name": "A", "ingredients_list": ["舊成分 A"]},
            {"name": "B", "ingredients_list": ["舊成分 B"]},
        ]
    )

    assert result == {0: ["玻尿酸"], 1: ["神經醯胺"]}
    assert len(calls) == 1
    assert calls[0]["model"] == product_agent.TERRA_MODEL
    assert calls[0]["temperature"] == 0.0
    assert len(calls[0]["messages"]) == 2


def test_batch_ingredient_extraction_falls_back_without_retry(monkeypatch):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise product_agent.ChatCompletionError("busy")

    monkeypatch.setenv("PRODUCT_INGREDIENT_EXTRACTOR", "terra")
    monkeypatch.setattr(product_agent._terra_client, "complete", fail)
    monkeypatch.setattr(product_agent, "_log", lambda *args, **kwargs: None)

    result = product_agent._extract_ingredients_batch_taide(
        [{"name": "A", "ingredients_list": ["玻尿酸", "神經醯胺"]}]
    )

    assert result == {0: ["玻尿酸", "神經醯胺"]}
    assert len(calls) == 1


def test_batch_copy_generation_delegates_to_shared_chat_client(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            [{"copy": "適合敏感肌的保濕選擇。", "recommendation_reason": "符合保濕需求"}],
            ensure_ascii=False,
        )

    monkeypatch.setenv("PRODUCT_COPY_USE_LLM", "1")
    monkeypatch.setattr(product_agent._terra_client, "complete", fake_complete)
    products = [
        {
            "name": "測試精華",
            "ingredients": ["玻尿酸"],
            "concern_hits": 1,
            "matched_claim_terms": ["保濕"],
            "price": 880,
            "is_partner": False,
            "over_budget": False,
        }
    ]

    result = product_agent._batch_generate_rank_content(
        products,
        skin_type="sensitive",
        concerns=["保濕"],
        warnings=[],
        memory_context="",
    )

    assert result is products
    assert result[0]["copy"] == "適合敏感肌的保濕選擇。"
    assert "保濕" in result[0]["recommendation_reason"]
    assert len(calls) == 1
    assert calls[0]["model"] == product_agent.TERRA_MODEL
    prompt = calls[0]["messages"][0]["content"]
    assert "一般消費者容易理解" in prompt
    assert "配方資料已可核對" in prompt
    assert "不可暗示完整配方已驗證" in prompt
