from __future__ import annotations

from types import SimpleNamespace

from agents.root import orchestrator


def _intent():
    return SimpleNamespace(
        skin_type=SimpleNamespace(value="sensitive"),
        concern=["保濕", "泛紅"],
    )


def test_rank_feedback_delegates_to_shared_terra_client(monkeypatch):
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs)
        return "我理解您想優先考量清爽度。"

    monkeypatch.setattr(orchestrator._terra_client, "complete", fake_complete)

    result = orchestrator._handle_rank_feedback(
        "我想要更清爽的",
        [
            {"name": "商品 A", "price": 880},
            {"name": "商品 B", "price": 650},
        ],
        _intent(),
    )

    assert result.startswith("我理解您想優先考量清爽度。")
    assert "請問您希望優先考慮哪個方向" in result
    assert len(calls) == 1
    assert calls[0]["model"] == orchestrator.TERRA_MODEL
    assert calls[0]["max_tokens"] == 200
    assert calls[0]["temperature"] == 0.5
    assert calls[0]["timeout_seconds"] == 20
    assert len(calls[0]["messages"]) == 2
    assert "商品 A" in calls[0]["messages"][1]["content"]


def test_rank_feedback_failure_uses_existing_fallback_without_retry(monkeypatch):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("busy")

    monkeypatch.setattr(orchestrator._terra_client, "complete", fail)

    result = orchestrator._handle_rank_feedback(
        "我想要更便宜的",
        [{"name": "商品 A", "price": 880}],
        _intent(),
    )

    assert "感謝您的回饋" in result
    assert "價格" in result
    assert len(calls) == 1
