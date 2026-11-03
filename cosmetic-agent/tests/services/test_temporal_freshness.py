import time

import pytest

from app.services.temporal_freshness import (
    build_freshness_guard_reply,
    classify_freshness_query,
)


@pytest.mark.parametrize(
    ("message", "domain", "freshness"),
    [
        ("現在天氣幾度", "weather", "live"),
        ("昨天臺北的天氣如何", "weather", "historical"),
        ("明天高雄會下雨嗎", "weather", "forecast"),
        ("天氣如何影響皮膚", "weather", "stable"),
        ("臺中現在空氣品質如何", "air_quality", "live"),
        ("這款保濕精華還有貨嗎", "product_availability", "live"),
        ("這款防曬目前價格多少", "product_price", "live"),
        ("最新化妝品召回公告", "safety_notice", "live"),
        ("化妝品禁用成分規定", "cosmetic_regulation", "versioned"),
        ("化妝品最新消息", "beauty_news", "live"),
    ],
)
def test_classifies_beauty_freshness(message, domain, freshness):
    decision = classify_freshness_query(message)
    assert decision is not None
    assert (decision["domain"], decision["freshness"]) == (domain, freshness)
    assert decision["requires_external_data"] is (freshness != "stable")


@pytest.mark.parametrize(
    "message",
    [
        "現在國道一號路況", "目前火車誤點嗎", "目前美元匯率", "今天比賽比分",
        "現任衛福部長是誰", "最新軟體版本", "今天有什麼新聞", "這張票有貨嗎",
        "我昨天說過我的臉很乾嗎？", "我目前臉很乾，要擦什麼？",
        "保濕產品價格怎麼比較", "最近皮膚很乾",
    ],
)
def test_non_beauty_or_non_live_context_does_not_enter_freshness_path(message):
    assert classify_freshness_query(message) is None


def test_weather_requires_location_instead_of_ip_guessing():
    decision = classify_freshness_query("現在天氣幾度")
    assert decision is not None
    assert decision["missing_slots"] == ["location"]
    assert "縣市" in build_freshness_guard_reply(decision)


@pytest.mark.parametrize(
    ("message", "location"),
    [("台北現在天氣", "臺北市"), ("新北市現在天氣", "新北市"), ("新竹縣目前氣溫", "新竹縣")],
)
def test_location_normalization(message, location):
    decision = classify_freshness_query(message)
    assert decision is not None
    assert decision["slots"]["location"] == location


def test_temperature_question_requests_only_temperature():
    decision = classify_freshness_query("台北現在幾度")
    assert decision is not None
    assert decision["answer_intent"] == "weather_observation"
    assert decision["requested_fields"] == ["temperature_c"]


def test_skincare_weather_question_requests_evidence_and_advice():
    decision = classify_freshness_query("依今天台北天氣調整我的保養")
    assert decision is not None
    assert decision["answer_intent"] == "skincare_adjustment"
    assert decision["requested_fields"] == [
        "temperature_c", "humidity_percent", "uv_index",
        "precipitation_probability_percent", "skincare_advice",
    ]


def test_product_tasks_preserve_requested_answer_shape():
    stock = classify_freshness_query("這款保濕精華還有貨嗎")
    price = classify_freshness_query("這款防曬目前價格多少")
    assert stock and stock["requested_fields"] == ["availability", "checked_at"]
    assert price and price["requested_fields"] == ["price", "currency", "checked_at"]


def test_explicit_calendar_date_is_historical():
    decision = classify_freshness_query("2025年7月臺北天氣")
    assert decision is not None
    assert decision["freshness"] == "historical"


def test_fast_gate_has_sub_millisecond_p95_cost():
    samples = [
        "現在天氣幾度", "依今天台北天氣調整我的保養", "目前美元匯率",
        "這款保濕精華還有貨嗎", "我目前臉很乾",
    ]
    timings = []
    for _ in range(500):
        started = time.perf_counter_ns()
        for sample in samples:
            classify_freshness_query(sample)
        timings.append((time.perf_counter_ns() - started) / len(samples))
    timings.sort()
    assert timings[int(len(timings) * 0.95)] < 1_000_000
