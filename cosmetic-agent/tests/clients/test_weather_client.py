from __future__ import annotations

import asyncio

import pytest

from app.clients import weather_client as wc
from app.clients.weather_client import AsyncWeatherClient, WeatherProviderError


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "current": {
                "time": "2026-10-02T15:00",
                "temperature_2m": 30.4,
                "relative_humidity_2m": 72,
                "weather_code": 3,
            },
            "hourly": {
                "time": ["2026-10-02T14:00", "2026-10-02T15:00"],
                "uv_index": [5.0, 4.1],
                "precipitation_probability": [20, 35],
            },
        }


class FakeHTTPClient:
    def __init__(self, *, delay=0, error=False):
        self.calls = 0
        self.delay = delay
        self.error = error

    async def get(self, url, *, params):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("provider failed")
        return FakeResponse()


@pytest.mark.asyncio
async def test_observation_has_provenance_and_freshness_metadata():
    http = FakeHTTPClient()
    client = AsyncWeatherClient(client=http, ttl_seconds=600)
    result = await client.get_current("臺北市")
    assert result["temperature_c"] == 30.4
    assert result["humidity_percent"] == 72
    assert result["uv_index"] == 4.1
    assert result["dataset_id"] == "open-meteo:forecast"
    assert result["source"] == "Open-Meteo"
    assert result["observed_at"] < result["valid_until"]


@pytest.mark.asyncio
async def test_cache_avoids_duplicate_provider_requests():
    http = FakeHTTPClient()
    client = AsyncWeatherClient(client=http)
    first = await client.get_current("臺北市")
    second = await client.get_current("臺北市")
    assert http.calls == 1
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"


@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_requests():
    http = FakeHTTPClient(delay=0.02)
    client = AsyncWeatherClient(client=http)
    results = await asyncio.gather(*(client.get_current("高雄市") for _ in range(8)))
    assert http.calls == 1
    assert len(results) == 8
    assert client.metrics()["coalesced"] == 7


@pytest.mark.asyncio
async def test_expired_entry_is_revalidated(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(wc.time, "monotonic", lambda: clock[0])
    http = FakeHTTPClient()
    client = AsyncWeatherClient(client=http, ttl_seconds=1)
    await client.get_current("臺北市")
    clock[0] = 102.0
    await client.get_current("臺北市")
    assert http.calls == 2


@pytest.mark.asyncio
async def test_circuit_breaker_stops_repeated_failed_calls():
    http = FakeHTTPClient(error=True)
    client = AsyncWeatherClient(client=http, failure_threshold=2)
    with pytest.raises(WeatherProviderError):
        await client.get_current("臺北市")
    with pytest.raises(WeatherProviderError):
        await client.get_current("臺北市")
    with pytest.raises(WeatherProviderError, match="circuit"):
        await client.get_current("臺北市")
    assert http.calls == 2
