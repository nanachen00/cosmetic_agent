"""Fast, beauty-scoped routing for time-sensitive questions.

The classifier is deterministic and performs no model or network call.  It
returns both freshness requirements and the answer shape requested by the user.
"""
from __future__ import annotations

import re
from typing import Literal, TypedDict

FreshnessClass = Literal["live", "historical", "forecast", "versioned", "stable"]


class FreshnessDecision(TypedDict):
    domain: str
    freshness: FreshnessClass
    answer_intent: str
    requested_fields: list[str]
    requires_external_data: bool
    required_slots: list[str]
    missing_slots: list[str]
    slots: dict[str, str]
    max_age_seconds: int | None
    reason_codes: list[str]
    confidence: float


_CURRENT_MARKERS = (
    "現在", "目前", "今天", "今日", "即時", "最新", "剛剛", "此刻", "當下",
    "現況", "幾度", "還有貨", "有現貨", "缺貨", "售價", "優惠", "多少錢",
)
_HISTORICAL_MARKERS = (
    "昨天", "前天", "上週", "上個月", "去年", "前年", "當時", "那天", "歷史",
    "曾經", "過去", "先前", "以前",
)
_FORECAST_MARKERS = (
    "明天", "後天", "未來", "稍後", "下週", "下個月", "預報", "會不會",
)
_DEFINITIONAL_MARKERS = (
    "是什麼", "意思", "定義", "原理", "怎麼形成", "如何影響", "有什麼關係",
    "為什麼會", "差別", "機制",
)
_BEAUTY_ANCHORS = (
    "美妝", "保養", "化妝", "化粧", "護膚", "皮膚", "膚況", "防曬", "乳液",
    "乳霜", "精華", "面膜", "洗面", "潔顏", "藥妝", "藥膏", "成分", "品牌",
    "商品", "產品", "痘痘", "敏感肌", "酒糟", "粉刺", "毛孔",
)
_WEATHER_TERMS = ("天氣", "氣溫", "體感溫度", "幾度", "濕度", "紫外線", "uv", "降雨", "下雨", "風速")
_AIR_TERMS = ("空氣品質", "aqi", "pm2.5", "pm10", "空污")
_AVAILABILITY_TERMS = ("庫存", "有貨", "現貨", "缺貨", "補貨", "剩幾個")
_PRICE_TERMS = ("價格", "售價", "折扣", "優惠", "運費", "多少錢")
_SAFETY_TERMS = ("化妝品召回", "化粧品召回", "保養品召回", "藥品召回", "藥膏召回", "安全警訊", "藥證狀態")
_REGULATION_TERMS = ("化妝品法規", "化粧品法規", "禁用成分", "限用成分", "成分規定", "藥證", "許可證")
_BEAUTY_NEWS_TERMS = ("美妝新聞", "保養新聞", "化妝品最新消息", "化粧品最新消息", "食藥署化妝品公告")

_TAIWAN_LOCATIONS = (
    "基隆市", "臺北市", "台北市", "新北市", "桃園市", "新竹市", "新竹縣",
    "苗栗縣", "臺中市", "台中市", "彰化縣", "南投縣", "雲林縣", "嘉義市",
    "嘉義縣", "臺南市", "台南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣",
    "臺東縣", "台東縣", "澎湖縣", "金門縣", "連江縣", "馬祖", "臺灣", "台灣",
)
_LOCATION_ALIASES = {
    "台北": "臺北市", "臺北": "臺北市", "新北": "新北市", "桃園": "桃園市",
    "新竹": "新竹市", "苗栗": "苗栗縣", "台中": "臺中市", "臺中": "臺中市",
    "彰化": "彰化縣", "南投": "南投縣", "雲林": "雲林縣", "嘉義": "嘉義市",
    "台南": "臺南市", "臺南": "臺南市", "高雄": "高雄市", "屏東": "屏東縣",
    "宜蘭": "宜蘭縣", "花蓮": "花蓮縣", "台東": "臺東縣", "臺東": "臺東縣",
    "澎湖": "澎湖縣", "金門": "金門縣", "馬祖": "連江縣",
}
_EXPLICIT_DATE = re.compile(r"(?:20\d{2}|19\d{2})[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?")
_NORMALIZE_TABLE = str.maketrans("", "", "，,。.!！?？、；;：:～~…")


def _normalize(text: str) -> str:
    return "".join(text.lower().split()).translate(_NORMALIZE_TABLE)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _extract_location(text: str) -> str | None:
    for location in _TAIWAN_LOCATIONS:
        if location in text:
            return _LOCATION_ALIASES.get(location, location.replace("台", "臺", 1))
    for alias, canonical in _LOCATION_ALIASES.items():
        if alias in text:
            return canonical
    return None


def _weather_answer_shape(text: str) -> tuple[str, list[str]]:
    skincare = _contains_any(text, _BEAUTY_ANCHORS) or _contains_any(
        text, ("擦什麼", "怎麼擦", "怎麼保養", "適合用", "需要調整")
    )
    if skincare:
        return "skincare_adjustment", [
            "temperature_c", "humidity_percent", "uv_index",
            "precipitation_probability_percent", "skincare_advice",
        ]
    fields: list[str] = []
    if _contains_any(text, ("氣溫", "幾度", "體感溫度")):
        fields.append("temperature_c")
    if "濕度" in text:
        fields.append("humidity_percent")
    if _contains_any(text, ("紫外線", "uv")):
        fields.append("uv_index")
    if _contains_any(text, ("降雨", "下雨")):
        fields.append("precipitation_probability_percent")
    if not fields:
        fields = ["temperature_c", "humidity_percent", "uv_index", "precipitation_probability_percent"]
    return "weather_observation", fields


def classify_freshness_query(text: str) -> FreshnessDecision | None:
    """Return a beauty-scoped freshness decision and requested answer fields."""
    raw = (text or "").strip()
    if not raw:
        return None
    normalized = _normalize(raw)
    beauty_related = _contains_any(normalized, _BEAUTY_ANCHORS)

    if _contains_any(normalized, _AIR_TERMS):
        domain, ttl = "air_quality", 600
        answer_intent = "air_quality_skin_risk" if beauty_related else "air_quality_observation"
        requested_fields = ["aqi", "pm2_5"] + (["skincare_advice"] if beauty_related else [])
    elif _contains_any(normalized, _WEATHER_TERMS):
        domain, ttl = "weather", 600
        answer_intent, requested_fields = _weather_answer_shape(normalized)
    elif beauty_related and _contains_any(normalized, _AVAILABILITY_TERMS):
        domain, ttl = "product_availability", 60
        answer_intent, requested_fields = "product_availability", ["availability", "checked_at"]
    elif beauty_related and _contains_any(normalized, _PRICE_TERMS):
        domain, ttl = "product_price", 300
        answer_intent, requested_fields = "product_price", ["price", "currency", "checked_at"]
    elif _contains_any(normalized, _SAFETY_TERMS):
        domain, ttl = "safety_notice", 3600
        answer_intent, requested_fields = "product_safety_status", ["status", "notice_date", "source"]
    elif _contains_any(normalized, _REGULATION_TERMS):
        domain, ttl = "cosmetic_regulation", 86400
        answer_intent, requested_fields = "ingredient_regulation", ["legal_status", "effective_date", "source"]
    elif _contains_any(normalized, _BEAUTY_NEWS_TERMS):
        domain, ttl = "beauty_news", 300
        answer_intent, requested_fields = "beauty_update", ["headline", "published_at", "source"]
    else:
        return None

    definitional = _contains_any(normalized, _DEFINITIONAL_MARKERS)
    current = _contains_any(normalized, _CURRENT_MARKERS)
    historical = _contains_any(normalized, _HISTORICAL_MARKERS)
    forecast = _contains_any(normalized, _FORECAST_MARKERS)
    explicit_date = bool(_EXPLICIT_DATE.search(raw))
    if (
        domain == "product_price"
        and not current
        and not historical
        and not forecast
        and not explicit_date
        and _contains_any(normalized, ("比較", "差異", "怎麼選", "哪個比較"))
    ):
        return None
    short_observation = domain in {"weather", "air_quality"} and len(normalized) <= 12 and not definitional

    if definitional and not current and not historical and not forecast:
        freshness: FreshnessClass = "stable"
        external, ttl = False, None
    elif historical or explicit_date:
        freshness, external, ttl = "historical", True, None
    elif forecast:
        freshness, external = "forecast", True
    elif current or short_observation or domain in {"product_availability", "product_price"}:
        freshness, external = "live", True
    elif domain in {"safety_notice", "cosmetic_regulation", "beauty_news"}:
        freshness, external = "versioned", True
    else:
        freshness, external, ttl = "stable", False, None

    slots: dict[str, str] = {}
    required_slots: list[str] = []
    if domain in {"weather", "air_quality"} and external:
        required_slots.append("location")
        location = _extract_location(raw)
        if location:
            slots["location"] = location
    missing_slots = [slot for slot in required_slots if slot not in slots]

    reasons = [f"domain:{domain}", f"freshness:{freshness}", f"intent:{answer_intent}"]
    if current:
        reasons.append("explicit_current_marker")
    if historical or explicit_date:
        reasons.append("historical_time_anchor")
    if forecast:
        reasons.append("future_time_anchor")
    if short_observation and not current:
        reasons.append("environment_observation_defaults_to_now")
    if missing_slots:
        reasons.append("missing_required_slot")

    confidence = 0.99 if current or historical or forecast or explicit_date else 0.94
    if freshness == "stable":
        confidence = 0.9
    return {
        "domain": domain,
        "freshness": freshness,
        "answer_intent": answer_intent,
        "requested_fields": requested_fields,
        "requires_external_data": external,
        "required_slots": required_slots,
        "missing_slots": missing_slots,
        "slots": slots,
        "max_age_seconds": ttl,
        "reason_codes": reasons,
        "confidence": confidence,
    }


def build_freshness_guard_reply(decision: FreshnessDecision) -> str:
    if decision["domain"] in {"weather", "air_quality"} and "location" in decision["missing_slots"]:
        return "要查即時環境資料，我還需要你所在的縣市（例如臺北市或高雄市）；我不會用 IP 猜位置。"
    if decision["freshness"] == "historical":
        return "這題需要指定時間的歷史資料；可靠資料暫時無法取得，所以我先不猜測。"
    labels = {
        "product_availability": "商品庫存",
        "product_price": "商品價格",
        "safety_notice": "產品安全公告",
        "cosmetic_regulation": "化妝品法規",
        "beauty_news": "美妝官方消息",
        "air_quality": "空氣品質",
        "weather": "天氣",
    }
    label = labels.get(decision["domain"], "即時")
    return f"{label}資料來源暫時無法取得；為避免把舊資料當成現況，我先不猜測。"
