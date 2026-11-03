"""Prepare fresh external evidence before synchronous agent execution."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, TypedDict

from app.clients.terra_completion_client import TerraCompletionClient
from app.clients.weather_client import WeatherProviderError, weather_client
from app.services.temporal_freshness import FreshnessDecision, classify_freshness_query


class FreshnessContext(TypedDict, total=False):
    decision: FreshnessDecision
    observation: dict[str, Any]
    provider_error: str


async def prepare_freshness_context(message: str) -> FreshnessContext | None:
    if os.getenv("TEMPORAL_FRESHNESS_GUARD_ENABLED", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    decision = classify_freshness_query(message)
    if not decision or not decision["requires_external_data"]:
        return None
    context: FreshnessContext = {"decision": decision}
    if decision["domain"] != "weather" or decision["missing_slots"]:
        return context
    if decision["freshness"] != "live":
        context["provider_error"] = "unsupported_temporal_scope"
        return context
    try:
        context["observation"] = await weather_client.get_current(
            decision["slots"]["location"]
        )
    except WeatherProviderError:
        context["provider_error"] = "provider_unavailable"
    return context


def _skincare_guidance(temperature: float, humidity: int, uv: float, rain: int) -> list[str]:
    guidance: list[str] = []
    if humidity <= 40:
        guidance.append("環境偏乾，可用含甘油／玻尿酸的補水層，再以神經醯胺乳霜減少水分散失")
    elif humidity <= 55:
        guidance.append("濕度稍低，可用保濕精華搭配薄層乳液或乳霜")
    elif humidity >= 80:
        guidance.append("濕度很高，建議使用清爽凝乳並減少厚重、封閉性的多層疊擦")
    elif humidity >= 70:
        guidance.append("環境偏濕，保濕以輕薄、不黏膩為主")
    else:
        guidance.append("濕度適中，維持平常的保濕層次即可")
    if temperature >= 32:
        guidance.append("氣溫偏高，避免過度厚重，出汗後溫和清潔並補擦防曬")
    elif temperature <= 16:
        guidance.append("氣溫偏低，可提高乳霜滋潤度並留意乾燥刺激")
    if uv >= 8:
        guidance.append("UV 很高，外出使用足量廣譜防曬並按活動情況補擦")
    elif uv >= 3:
        guidance.append("日間仍建議使用廣譜防曬")
    if rain >= 60:
        guidance.append("降雨機率偏高，戶外可優先選擇耐水型防曬")
    return guidance


def build_weather_reply(context: FreshnessContext) -> str | None:
    observation = context.get("observation")
    if not observation:
        return None
    decision = context.get("decision") or {}
    requested = set(decision.get("requested_fields") or (
        "temperature_c", "humidity_percent", "uv_index",
        "precipitation_probability_percent", "skincare_advice",
    ))
    temperature = float(observation["temperature_c"])
    humidity = int(observation["humidity_percent"])
    uv = float(observation["uv_index"])
    rain = int(observation["precipitation_probability_percent"])

    facts: list[str] = []
    if "temperature_c" in requested:
        facts.append(f"氣溫 {temperature:g}°C")
    if "humidity_percent" in requested:
        facts.append(f"濕度 {humidity}%")
    if "uv_index" in requested:
        facts.append(f"UV {uv:g}")
    if "precipitation_probability_percent" in requested:
        facts.append(f"降雨機率 {rain}%")

    observed = datetime.fromisoformat(str(observation["observed_at"]))
    prefix = f"{observation['location']}目前" + "、".join(facts)
    if not facts:
        prefix = f"{observation['location']}目前的即時資料已取得"
    if "skincare_advice" in requested:
        guidance = _skincare_guidance(temperature, humidity, uv, rain)
        prefix += "。" + "；".join(guidance[:3])
    return (
        f"{prefix}（資料時間 {observed.strftime('%H:%M')}）。"
        f"資料來源：{observation['source']}。"
    )


def generate_weather_advice(
    *,
    location: str,
    temperature: float,
    humidity: int,
    uv: float,
    rain: int,
    condition: str,
) -> str:
    """Generate one concise skincare suggestion from trusted weather facts."""
    client = TerraCompletionClient.from_env()
    reply = client.complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "你是皮膚保養助理。請根據提供的即時天氣數據，給一則繁體中文、"
                    "一句話、30至55字、可執行且不做醫療診斷的日常保養建議。"
                    "必須同時考量溫度、濕度、UV與降雨機率。"
                    "不要重述地區、天氣狀況或任何天氣數值，直接從保養動作開始，"
                    "只輸出建議。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"地區：{location}；天氣：{condition}；氣溫：{temperature:g}°C；"
                    f"濕度：{humidity}%；UV：{uv:g}；降雨機率：{rain}%"
                ),
            },
        ],
        max_tokens=100,
        timeout_seconds=8,
    )
    normalized = " ".join(reply.split()).strip('『』「」"\'')
    if not normalized:
        raise RuntimeError("Terra returned empty weather advice")
    return normalized[:100]
