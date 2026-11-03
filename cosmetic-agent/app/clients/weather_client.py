"""Async Open-Meteo client with bounded freshness and coalesced requests."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import httpx


class WeatherProviderError(RuntimeError):
    pass


class WeatherObservation(TypedDict):
    location: str
    temperature_c: float
    humidity_percent: int
    uv_index: float
    precipitation_probability_percent: int
    weather_code: int
    observed_at: str
    retrieved_at: str
    valid_until: str
    dataset_id: str
    source: str
    source_url: str
    cache_status: str


_LOCATION_COORDINATES: dict[str, tuple[float, float]] = {
    "基隆市": (25.1276, 121.7392), "臺北市": (25.0330, 121.5654),
    "新北市": (25.0120, 121.4657), "桃園市": (24.9937, 121.3010),
    "新竹市": (24.8138, 120.9675), "新竹縣": (24.8387, 121.0177),
    "苗栗縣": (24.5602, 120.8214), "臺中市": (24.1477, 120.6736),
    "彰化縣": (24.0518, 120.5161), "南投縣": (23.9609, 120.9719),
    "雲林縣": (23.7092, 120.4313), "嘉義市": (23.4801, 120.4491),
    "嘉義縣": (23.4518, 120.2555), "臺南市": (22.9999, 120.2269),
    "高雄市": (22.6273, 120.3014), "屏東縣": (22.5519, 120.5488),
    "宜蘭縣": (24.7021, 121.7378), "花蓮縣": (23.9911, 121.6112),
    "臺東縣": (22.7554, 121.1500), "澎湖縣": (23.5712, 119.5793),
    "金門縣": (24.4494, 118.3767), "連江縣": (26.1605, 119.9517),
}


class AsyncWeatherClient:
    endpoint = "https://api.open-meteo.com/v1/forecast"
    dataset_id = "open-meteo:forecast"

    def __init__(
        self,
        *,
        client: Any | None = None,
        ttl_seconds: int = 600,
        timeout_seconds: float = 4.0,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "BLINK-AI/1.0"},
        )
        self._owns_client = client is None
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._failure_threshold = max(1, int(failure_threshold))
        self._recovery_seconds = max(1.0, float(recovery_seconds))
        self._cache: dict[str, tuple[float, WeatherObservation]] = {}
        self._inflight: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._metrics = {"cache_hit": 0, "cache_miss": 0, "coalesced": 0}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    async def get_current(self, location: str) -> WeatherObservation:
        canonical = str(location or "").strip()
        coordinates = _LOCATION_COORDINATES.get(canonical)
        if coordinates is None:
            raise WeatherProviderError(f"unsupported location: {canonical}")

        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(canonical)
            if cached and cached[0] > now:
                self._metrics["cache_hit"] += 1
                return {**cached[1], "cache_status": "hit"}
            if self._open_until > now:
                raise WeatherProviderError("weather provider circuit is open")

            entry = self._inflight.get(canonical)
            if entry is None:
                self._metrics["cache_miss"] += 1
                task = asyncio.create_task(self._fetch_and_store(canonical, coordinates))
                entry = {"task": task, "waiters": 0}
                self._inflight[canonical] = entry
            else:
                self._metrics["coalesced"] += 1
            entry["waiters"] += 1
            task = entry["task"]

        try:
            result = await asyncio.shield(task)
            return dict(result)
        finally:
            async with self._lock:
                active = self._inflight.get(canonical)
                if active is not None and active["task"] is task:
                    active["waiters"] -= 1
                    if active["waiters"] <= 0:
                        if not task.done():
                            task.cancel()
                        self._inflight.pop(canonical, None)

    async def _fetch_and_store(
        self,
        location: str,
        coordinates: tuple[float, float],
    ) -> WeatherObservation:
        latitude, longitude = coordinates
        params = {
            "latitude": str(latitude),
            "longitude": str(longitude),
            "current": "temperature_2m,relative_humidity_2m,weather_code",
            "hourly": "uv_index,precipitation_probability",
            "timezone": "Asia/Taipei",
            "forecast_days": "1",
        }
        try:
            response = await asyncio.wait_for(
                self._client.get(self.endpoint, params=params),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            observation = self._parse(location, response.json())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self._lock:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._open_until = time.monotonic() + self._recovery_seconds
            raise WeatherProviderError("weather provider request failed") from error

        async with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._cache[location] = (
                time.monotonic() + self._ttl_seconds,
                observation,
            )
        return observation

    def _parse(self, location: str, data: dict[str, Any]) -> WeatherObservation:
        current = data["current"]
        hourly = data.get("hourly") or {}
        times = list(hourly.get("time") or [])
        current_time = str(current["time"])
        nearest = 0
        if times:
            target = _parse_taipei_time(current_time)
            nearest = min(
                range(len(times)),
                key=lambda index: abs((_parse_taipei_time(str(times[index])) - target).total_seconds()),
            )
        uv_values = list(hourly.get("uv_index") or [])
        rain_values = list(hourly.get("precipitation_probability") or [])
        retrieved = datetime.now(ZoneInfo("Asia/Taipei"))
        valid_until = retrieved + timedelta(seconds=self._ttl_seconds)
        return {
            "location": location,
            "temperature_c": round(float(current["temperature_2m"]), 1),
            "humidity_percent": round(float(current["relative_humidity_2m"])),
            "uv_index": round(float(uv_values[nearest] if nearest < len(uv_values) else 0), 1),
            "precipitation_probability_percent": round(float(rain_values[nearest] if nearest < len(rain_values) else 0)),
            "weather_code": int(current["weather_code"]),
            "observed_at": _parse_taipei_time(current_time).isoformat(),
            "retrieved_at": retrieved.isoformat(),
            "valid_until": valid_until.isoformat(),
            "dataset_id": self.dataset_id,
            "source": "Open-Meteo",
            "source_url": self.endpoint,
            "cache_status": "miss",
        }


def _parse_taipei_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return parsed


weather_client = AsyncWeatherClient()
