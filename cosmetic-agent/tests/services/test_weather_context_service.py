from __future__ import annotations

import pytest

from app.services import weather_context_service as service


def observation():
    return {
        "location": "臺北市",
        "temperature_c": 33.0,
        "humidity_percent": 38,
        "uv_index": 9.0,
        "precipitation_probability_percent": 70,
        "weather_code": 2,
        "observed_at": "2026-10-02T15:00:00+08:00",
        "retrieved_at": "2026-10-02T15:01:00+08:00",
        "valid_until": "2026-10-02T15:11:00+08:00",
        "dataset_id": "open-meteo:forecast",
        "source": "Open-Meteo",
        "source_url": "https://api.open-meteo.com/v1/forecast",
        "cache_status": "miss",
    }


@pytest.mark.asyncio
async def test_missing_location_does_not_call_provider(monkeypatch):
    called = []
    async def fake(location):
        called.append(location)
    monkeypatch.setattr(service.weather_client, "get_current", fake)
    context = await service.prepare_freshness_context("現在天氣幾度")
    assert context is not None
    assert context["decision"]["missing_slots"] == ["location"]
    assert called == []


@pytest.mark.asyncio
async def test_live_weather_fetches_structured_evidence(monkeypatch):
    async def fake(location):
        assert location == "臺北市"
        return observation()
    monkeypatch.setattr(service.weather_client, "get_current", fake)
    context = await service.prepare_freshness_context("台北現在天氣幾度")
    assert context is not None
    assert context["observation"]["dataset_id"] == "open-meteo:forecast"


@pytest.mark.asyncio
async def test_historical_weather_is_not_fabricated(monkeypatch):
    called = []
    async def fake(location):
        called.append(location)
    monkeypatch.setattr(service.weather_client, "get_current", fake)
    context = await service.prepare_freshness_context("昨天台北天氣")
    assert context is not None
    assert context["provider_error"] == "unsupported_temporal_scope"
    assert called == []


def test_reply_is_grounded_and_bounded():
    reply = service.build_weather_reply({"observation": observation()})
    assert reply is not None
    assert "33°C" in reply and "濕度 38%" in reply
    assert "UV 9" in reply and "資料時間 15:00" in reply
    assert "Open-Meteo" in reply
    assert "玻尿酸" in reply and "防曬" in reply

@pytest.mark.asyncio
async def test_feature_flag_disables_provider_and_routing(monkeypatch):
    called = []
    async def fake(location):
        called.append(location)
    monkeypatch.setenv("TEMPORAL_FRESHNESS_GUARD_ENABLED", "0")
    monkeypatch.setattr(service.weather_client, "get_current", fake)
    assert await service.prepare_freshness_context("台北現在天氣") is None
    assert called == []


def test_temperature_answer_omits_unrequested_metrics():
    decision = service.classify_freshness_query("台北現在幾度")
    reply = service.build_weather_reply({"decision": decision, "observation": observation()})
    assert reply is not None
    assert "氣溫 33°C" in reply
    assert "濕度" not in reply and "UV" not in reply and "降雨機率" not in reply
    assert "玻尿酸" not in reply


def test_skincare_answer_uses_environment_evidence():
    decision = service.classify_freshness_query("依今天台北天氣調整我的保養")
    reply = service.build_weather_reply({"decision": decision, "observation": observation()})
    assert reply is not None
    assert "氣溫 33°C" in reply and "濕度 38%" in reply and "UV 9" in reply
    assert "玻尿酸" in reply and "防曬" in reply
