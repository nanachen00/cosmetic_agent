"""
藥妝保養品推薦 Agent — 內部 Orchestration 版本

內部決策流程：
1. 向量搜尋（Neo4j）
2. 商品分流：partner → graph_search / external → 既有成分資料清洗
3. A2A → 知識庫 Agent（有成分才查，查法規 allowed/banned claims）
4. 過濾禁忌成分
5. 排序（安全與預算 → 需求相關性 → partner 僅作同分條件）
6. copywriting_tool 生成合規文案
7. 回傳結構化推薦列表
"""
import sys
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')

import os
import re
import time
import warnings
warnings.filterwarnings("ignore")

from core.observability import observe_pipeline_stage
from app.clients.embedding_client import (
    EmbeddingClient,
    EmbeddingServiceError,
)
from app.clients.terra_completion_client import TerraCompletionClient

from neo4j import GraphDatabase
from schemas.intent import IntentParseResult
from tools.search.graph_search import graph_search, format_graph_result
from tools.search.product_web_search import is_trending_product_query
from tools.agent.copywriting_tool import generate_copy
from chatgpt.terra_client import generate_product_intro
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()
TERRA_MODEL = os.getenv("TERRA_MODEL", "gpt-5.6-terra")
_terra_client = TerraCompletionClient.from_env()
_embedding_client = EmbeddingClient.from_env()
EMBEDDING_BASE_URL = _embedding_client.base_url
EMBEDDING_MODEL = _embedding_client.model
EMBEDDING_TIMEOUT_SECONDS = _embedding_client.timeout_seconds

# The imported Olive Young catalogue uses USD, while the Taiwan catalogues use
# TWD.  Keep the conversion configurable, but use the latest Bank of Taiwan
# spot selling rate available when this change was made (2026-09-18).
_CATALOG_SOURCE_CURRENCIES = {
    "oliveyoung": "USD",
}
_TWD_EXCHANGE_RATES = {
    "TWD": 1.0,
    "USD": float(os.getenv("PRODUCT_USD_TWD_RATE", "31.855")),
}

CATEGORY_MAP = {
    "面膜":    ["面膜"],
    "精華液":  ["精華液"],
    "化妝水":  ["化妝水"],
    "乳液":    ["乳液", "乳霜"],
    "乳霜":    ["乳霜", "乳液"],
    "洗面乳":  ["洗面乳"],
    "防曬乳":  ["防曬乳"],
    "保濕棉片":["保濕棉片"],
    "acne_patch": ["acne_patch", "痘痘貼", "痘痘貼片", "暗瘡貼"],
    "spot_care": ["spot_care", "局部調理", "痘痘凝膠", "痘疤凝膠", "淡斑精華"],
    "eye_care": ["eye_care", "眼周保養", "眼霜", "眼膜", "眼部精華"],
    "makeup_remover": ["makeup_remover", "卸妝油", "卸妝水", "卸妝乳", "卸妝膏"],
    "pore_strip": ["pore_strip", "粉刺貼"],
    "exfoliant": ["exfoliant", "酸類調理", "去角質"],
    "lip_care": ["lip_care", "唇部保養", "護唇膏", "潤唇膏", "唇膜"],
    "不明":    None,
}

_PRODUCT_TYPE_BY_CATEGORY = {
    "洗面乳": "cleanser",
    "化妝水": "toner_mist",
    "面膜": "mask",
    "精華液": "serum",
    "乳液": "lotion",
    "乳霜": "cream_gel",
    "保濕棉片": "toner_pad",
    "防曬乳": "sunscreen",
    "acne_patch": "acne_patch",
    "pore_strip": "pore_strip",
    "spot_care": "spot_care",
    "exfoliant": "exfoliant",
    "eye_care": "eye_care",
    "lip_care": "lip_care",
    "makeup_remover": "makeup_remover",
}

# Most imported catalogue rows use the broad category ``臉部保養``.  A strict
# category equality filter therefore made 8k+ valid products impossible to
# recommend.  These name hints are retrieval constraints (not claim evidence):
# they only decide whether a row can enter the semantic ranking pool.
_CATEGORY_NAME_KEYWORDS = {
    "洗面乳": ["洗面", "潔面", "洗顏", "潔顏", "潔淨露", "洗卸", "洗臉"],
    "面膜": ["面膜", "凍膜", "泥膜"],
    "精華液": ["精華", "安瓶"],
    "化妝水": ["化妝水", "爽膚水", "機能水", "化粧水"],
    "乳液": ["乳液", "乳霜", "凝乳", "保濕乳", "修護乳"],
    "乳霜": ["乳霜", "面霜", "乳液", "凝霜", "保濕霜", "修護霜"],
    "防曬乳": ["防曬", "隔離乳", "防曬乳"],
    "保濕棉片": ["棉片"],
    "acne_patch": ["痘痘貼", "痘痘貼片", "暗瘡貼", "pimple patch"],
    "pore_strip": ["粉刺貼"],
    "exfoliant": ["酸類調理", "去角質"],
    "lip_care": ["護唇", "潤唇", "唇膜"],
    "spot_care": ["局部調理", "痘痘凝膠", "痘疤凝膠", "spot care"],
    "eye_care": ["眼周保養", "眼霜", "眼膜", "眼部精華", "eye care"],
    "makeup_remover": ["卸妝油", "卸妝水", "卸妝乳", "卸妝膏", "卸妝凝膠", "卸妝慕斯", "卸妝棉", "makeup remover"],
}

_model  = None
_driver = None


# ──────────────────────────────────────────
# 基礎工具
# ──────────────────────────────────────────

def product_stable_key(product: dict) -> str:
    """Return one key for search candidates, rendered products, and seen state."""
    for field in ("id", "_id"):
        value = product.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    normalized_name = " ".join(str(product.get("name") or "").split()).casefold()
    return f"name:{normalized_name}" if normalized_name else ""


def filter_unseen_products(products: list, excluded_product_ids=None) -> list:
    excluded_keys = {
        str(product_id).strip()
        for product_id in (excluded_product_ids or [])
        if str(product_id).strip()
    }
    return [
        product
        for product in products
        if product_stable_key(product) not in excluded_keys
    ]


def get_query_embedding(query: str) -> list[float]:
    """Return one BGE-M3 vector through the shared port-8002 client."""
    try:
        return _embedding_client.embed_query(query)
    except EmbeddingServiceError as exc:
        _log("embedding_unavailable", f"Dedicated embedding service unavailable: {exc}")
        raise RuntimeError("product embedding service unavailable") from exc


def get_driver():
    global _driver
    if _driver is None:
        missing_neo4j = [
            name
            for name, value in (
                ("NEO4J_URI", NEO4J_URI),
                ("NEO4J_USER", NEO4J_USER),
                ("NEO4J_PASSWORD", NEO4J_PASSWORD),
            )
            if not value
        ]
        if missing_neo4j:
            raise RuntimeError(
                "缺少必要的 Neo4j 環境變數：" + ", ".join(missing_neo4j)
            )
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
    return _driver


def _clean_ingredients(ing_list: list) -> list:
    """清洗 ingredients_list，過濾 concern ID 和過長描述"""
    if not ing_list:
        return []
    cleaned = []
    for item in ing_list:
        if not item:
            continue
        s = str(item).strip()
        if s.startswith('concern_'):
            continue
        if len(s) > 30:
            continue
        cleaned.append(s)
    return cleaned[:5]


def _parse_budget(budget) -> float:
    """從 budget_range 解析預算上限數字，解析失敗回傳 None"""
    if not budget:
        return None
    try:
        nums = re.findall(r'\d+\.?\d*', str(budget))
        if nums:
            return float(nums[-1])   # 取最後一個數字（「300元以內」→ 300）
    except (ValueError, TypeError):
        pass
    return None


def _normalize_catalog_price(product: dict) -> dict:
    """Return a catalogue row whose public price is always in New Taiwan dollars."""
    source = str(product.get("source") or "").strip().casefold()
    original_currency = _CATALOG_SOURCE_CURRENCIES.get(source, "TWD")
    original_price = product.get("price")
    try:
        numeric_price = float(original_price)
    except (TypeError, ValueError):
        product["price"] = None
        product["currency"] = "TWD"
        return product

    rate = _TWD_EXCHANGE_RATES.get(original_currency, 1.0)
    product["original_price"] = numeric_price
    product["original_currency"] = original_currency
    product["price"] = int(round(numeric_price * rate))
    product["currency"] = "TWD"
    return product


def _normalize_catalog_prices(products: list) -> list:
    return [_normalize_catalog_price(product) for product in products]


_UNKNOWN_BRANDS = {"", "unknown", "unknown brand", "未知品牌", "未標示"}

_INGREDIENT_DISPLAY_NAMES = {
    "glycerin": "甘油（Glycerin）",
    "hyaluronic": "玻尿酸（Hyaluronic Acid）",
    "hyaluronic acid": "玻尿酸（Hyaluronic Acid）",
    "sodium hyaluronate": "玻尿酸鈉（Sodium Hyaluronate）",
    "niacinamide": "菸鹼醯胺（Niacinamide）",
    "panthenol": "維他命原 B5（Panthenol）",
    "vitamin c": "維他命 C（Vitamin C）",
    "ascorbic": "維他命 C（Ascorbic Acid）",
    "tranexamic": "傳明酸（Tranexamic Acid）",
    "tranexamic acid": "傳明酸（Tranexamic Acid）",
    "alpha-arbutin": "α-熊果素（Alpha-Arbutin）",
    "arbutin": "熊果素（Arbutin）",
    "kojic acid": "麴酸（Kojic Acid）",
    "salicylic acid": "水楊酸（Salicylic Acid）",
    "azelaic acid": "壬二酸（Azelaic Acid）",
    "ceramide": "神經醯胺（Ceramide）",
    "squalane": "角鯊烷（Squalane）",
    "retinol": "視黃醇（Retinol）",
    "retinal": "視黃醛（Retinal）",
    "bakuchiol": "補骨脂酚（Bakuchiol）",
    "allantoin": "尿囊素（Allantoin）",
    "beta-glucan": "β-葡聚糖（Beta-Glucan）",
    "centella": "積雪草（Centella）",
    "macadamia oil": "澳洲堅果油（Macadamia Oil）",
    "mandelic": "杏仁酸（Mandelic Acid）",
    "zinc pca": "鋅 PCA（Zinc PCA）",
    "allantoin": "尿囊素（Allantoin）",
    "adenosine": "腺苷（Adenosine）",
    "peptide": "胜肽（Peptide）",
    "squalene": "角鯊烯（Squalene）",
}


def clean_ingredient_display(value: str, protected_terms=()) -> str:
    """Use Chinese ingredient names in customer copy, preserving product names and URLs."""
    text = str(value or "")
    if not text:
        return text

    protected = set(str(term) for term in protected_terms if str(term))
    protected.update(re.findall(r"https?://[^\s)）]+", text))
    placeholders = []
    for term in sorted(protected, key=len, reverse=True):
        if term in text:
            marker = f"\uE000{len(placeholders)}\uE001"
            text = text.replace(term, marker)
            placeholders.append((marker, term))

    chinese_names = {
        display.split("（", 1)[0] for display in _INGREDIENT_DISPLAY_NAMES.values()
    }
    for chinese in sorted(chinese_names, key=len, reverse=True):
        text = re.sub(
            rf"({re.escape(chinese)})\s*[（(]\s*[A-Za-z][A-Za-z0-9 ,+./-]*[)）]",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )
    for english, display in sorted(
        _INGREDIENT_DISPLAY_NAMES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        chinese = display.split("（", 1)[0]
        text = re.sub(
            rf"(?<![A-Za-z]){re.escape(english)}(?![A-Za-z])",
            lambda match: chinese,
            text,
            flags=re.IGNORECASE,
        )

    for marker, term in placeholders:
        text = text.replace(marker, term)
    return text

_RECOGNIZED_CHINESE_INGREDIENT_ALIASES = (
    "甘油",
    "玻尿酸",
    "透明質酸",
    "菸鹼醯胺",
    "煙醯胺",
    "泛醇",
    "維他命原b5",
    "維生素原b5",
    "維他命c",
    "維生素c",
    "傳明酸",
    "熊果素",
    "麴酸",
    "水楊酸",
    "壬二酸",
    "神經醯胺",
    "角鯊烷",
    "角鯊烯",
    "視黃醇",
    "視黃醛",
    "補骨脂酚",
    "尿囊素",
    "葡聚糖",
    "積雪草",
    "杏仁酸",
    "胜肽",
    "腺苷",
)


def _display_ingredient(value: str) -> str:
    text = str(value or "").strip()
    return clean_ingredient_display(_INGREDIENT_DISPLAY_NAMES.get(text.casefold(), text))

def _dedupe_display_ingredients(values: list) -> list[str]:
    deduped = []
    seen = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text or text.startswith("concern_") or len(text) > 80:
            continue
        display = _display_ingredient(text)
        key = " ".join(display.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(display)
    return deduped[:5]

def _has_recognized_ingredient(values: list) -> bool:
    for item in values or []:
        text = str(item or "").strip()
        normalized = text.casefold()
        if not text or text.startswith("concern_"):
            continue
        if any(alias in normalized for alias in _INGREDIENT_DISPLAY_NAMES):
            return True
        # Accept a discrete Chinese ingredient label, but not a product claim or
        # paragraph that merely mentions an ingredient-like marketing term.
        if len(text) <= 24 and any(
            alias in normalized for alias in _RECOGNIZED_CHINESE_INGREDIENT_ALIASES
        ):
            return True
        if re.search(r"[（(][A-Za-z][A-Za-z0-9 ,+\/-]{2,}[)）]", text):
            return True
    return False

_INGREDIENT_BENEFIT_TEXT = {"niacinamide": "可幫助減少黑色素傳遞並支援油脂調理", "vitamin c": "提供抗氧化與提亮支持", "ascorbic": "提供抗氧化與提亮支持", "tranexamic acid": "常用於改善膚色不均", "tranexamic": "常用於改善膚色不均", "alpha-arbutin": "可協助抑制黑色素生成", "arbutin": "可協助抑制黑色素生成", "kojic acid": "可提供抑制黑色素生成的保養支持", "azelaic acid": "兼具膚色與油脂調理作用", "salicylic": "可協助代謝老廢角質與疏通毛孔", "mandelic": "可溫和協助老廢角質代謝", "glycerin": "可吸附水分並支援角質層保濕", "hyaluronic": "可補充角質層水分", "panthenol": "可協助保濕與舒緩乾燥不適", "ceramide": "可支援肌膚屏障並減少水分散失", "centella": "可協助舒緩肌膚不適"}
def _ingredient_support_reason(term: str, concern: str) -> str:
    normalized = str(term or "").strip().casefold()
    benefit = _INGREDIENT_BENEFIT_TEXT.get(normalized) or f"可對應{concern}需求"
    return f"{_display_ingredient(term)}{benefit}"

def _product_source_urls(product: dict) -> list[str]:
    values = list(product.get("source_urls") or [])
    values.extend([product.get("source_url"), product.get("reference_url")])
    urls = []
    for value in values:
        url = str(value or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


def _product_data_quality(product: dict) -> str:
    # Keep basic catalog rows usable while verified rows retain priority.
    brand = str(product.get("brand") or "").strip()
    name = str(product.get("name") or "").strip()
    if not name or not brand or brand.casefold() in _UNKNOWN_BRANDS or not _product_source_urls(product):
        return "blocked"

    description = str(product.get("description") or "").strip()
    evidence_values = (
        list(product.get("ingredients_list") or [])
        + list(product.get("key_ingredients") or [])
    )
    evidence_level = str(product.get("ingredient_evidence_level") or "").strip()
    has_traceable_ingredient_evidence = (
        evidence_level in {"full_inci", "official_highlights"}
        and bool(evidence_values)
        and bool(str(product.get("ingredient_source_url") or "").strip())
    )
    has_verified_content = has_traceable_ingredient_evidence or _has_recognized_ingredient(evidence_values) or (
        str(product.get("source_type") or "").strip() == "admin_approved"
        and len(description) >= 20
    )

    if product.get("is_web_result"):
        web_summary = str(
            product.get("description")
            or product.get("snippet")
            or product.get("content")
            or ""
        ).strip()
        return "verified" if has_verified_content and len(web_summary) >= 20 else "basic"

    return "verified" if has_verified_content else "basic"


def _is_display_ready_product(product: dict) -> bool:
    return _product_data_quality(product) == "verified"


def _is_usable_product(product: dict) -> bool:
    return _product_data_quality(product) != "blocked"


_GENERIC_PRODUCT_CATEGORIES = {"", "臉部保養", "身體保養", "其他", "unknown"}
_CATEGORY_KEYWORDS = {
    "精華液": ("精華", "安瓶"),
    "化妝水": ("化妝水", "精華水", "爽膚水", "調理水"),
    "乳液": ("乳液", "凝乳"),
    "乳霜": ("乳霜", "面霜", "霜"),
    "面膜": ("面膜", "凍膜"),
    "洗面乳": ("洗面", "潔顏", "洗顏"),
    "防曬": ("防曬", "隔離乳", "uv", "spf"),
    "防曬乳": ("防曬", "隔離乳", "uv", "spf"),
    "acne_patch": ("痘痘貼", "痘痘貼片", "暗瘡貼", "patch"),
    "pore_strip": ("粉刺貼",),
    "exfoliant": ("酸類調理", "去角質"),
    "lip_care": ("護唇", "潤唇", "唇膜"),
    "spot_care": ("局部調理", "痘痘凝膠", "痘疤凝膠"),
    "eye_care": ("眼周保養", "眼霜", "眼膜", "眼部精華"),
    "makeup_remover": ("卸妝油", "卸妝水", "卸妝乳", "卸妝膏", "卸妝凝膠", "卸妝慕斯", "卸妝棉"),
}

_FACIAL_CATEGORIES = {
    "精華液", "化妝水", "乳液", "乳霜", "面膜", "洗面乳", "防曬", "防曬乳",
    "acne_patch", "pore_strip", "exfoliant", "spot_care", "eye_care", "makeup_remover",
}
_BODY_PRODUCT_MARKERS = (
    "身體", "身體乳", "身體凝乳", "身體保養", "體乳", "護手", "護足",
    "body lotion", "body cream", "body care", "hand cream", "foot cream",
)
_MAKEUP_PRODUCT_MARKERS = (
    "底妝", "粉底", "妝前", "潤色", "遮瑕", "妝效", "覆蓋效果", "色素", "primer", "foundation",
)

def _matches_requested_category(product: dict, requested_category: str = "") -> bool:
    wanted = str(requested_category or "").strip()
    if not wanted or wanted in _GENERIC_PRODUCT_CATEGORIES:
        return True
    actual = str(product.get("category") or "").strip()
    normalized = str(product.get("category_normalized") or "").strip()
    name = str(product.get("name") or "").strip()
    name_zh = str(product.get("name_zh") or "").strip()
    description = str(product.get("description") or "").strip()
    searchable_text = " ".join((actual, normalized, name, name_zh, description)).casefold()
    if wanted in _FACIAL_CATEGORIES and any(
        marker in searchable_text for marker in _BODY_PRODUCT_MARKERS
    ):
        return False
    if wanted in {"乳液", "乳霜"} and any(
        marker in searchable_text for marker in _MAKEUP_PRODUCT_MARKERS
    ):
        return False
    # product_type only identifies the dosage form.  A body lotion can still
    # carry the generic ``emulsion`` type, so explicit body/makeup evidence
    # must win before the stored-type fast path.
    expected_type = _PRODUCT_TYPE_BY_CATEGORY.get(wanted)
    stored_type = str(product.get("product_type") or "").strip()
    if expected_type and stored_type:
        return stored_type == expected_type
    if actual == wanted or wanted in actual or normalized == wanted:
        return True
    keywords = _CATEGORY_KEYWORDS.get(wanted, (wanted,))
    if actual and actual not in _GENERIC_PRODUCT_CATEGORIES:
        return any(keyword in actual for keyword in keywords)
    return any(keyword in name or keyword in name_zh for keyword in keywords)

def _log(stage: str, msg: str):
    print(f"[ProductAgent/{stage}] {msg}")


_RANKING_WEIGHT_KEYS = (
    "safety_score",
    "price_value_score",
    "review_score",
    "ingredient_match_score",
    "brand_score",
)


def _safe_score(value, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1.0 and number <= 5.0:
        number = number / 5.0
    return max(0.0, min(number, 1.0))


def _normalize_personalization_context(value: dict = None) -> dict:
    """Accept only the small set of fields needed by deterministic ranking."""
    context = value if isinstance(value, dict) else {}
    customer_type = str(context.get("customer_type") or "").strip()
    supplied = context.get("ranking_weights")
    has_preference_signal = bool(
        customer_type
        or context.get("preferred_brands")
        or context.get("avoided_brands")
        or context.get("preferred_effects")
        or context.get("preferred_textures")
        or context.get("budget_min")
        or context.get("budget_max")
    )
    if not has_preference_signal:
        return {"enabled": False}

    if not isinstance(supplied, dict):
        supplied = {
            "safety_score": 0.35,
            "price_value_score": 0.20,
            "review_score": 0.10,
            "ingredient_match_score": 0.25,
            "brand_score": 0.10,
        }

    weights = {}
    for key in _RANKING_WEIGHT_KEYS:
        try:
            weights[key] = max(float(supplied.get(key, 0)), 0.0)
        except (TypeError, ValueError):
            weights[key] = 0.0
    total = sum(weights.values())
    if total <= 0:
        return {"enabled": False}

    return {
        "enabled": True,
        "customer_type": customer_type or "known_preferences",
        "customer_type_label": str(
            context.get("customer_type_label") or customer_type or "已知偏好"
        ),
        "weights": {key: weights[key] / total for key in _RANKING_WEIGHT_KEYS},
        "preferred_brands": {
            str(item).strip().casefold()
            for item in (context.get("preferred_brands") or [])
            if str(item).strip()
        },
        "avoided_brands": {
            str(item).strip().casefold()
            for item in (context.get("avoided_brands") or [])
            if str(item).strip()
        },
    }


def _normalized_ingredient_text(values) -> str:
    return re.sub(
        r"[\s_\-()（）]+",
        "",
        " ".join(str(value or "") for value in values).casefold(),
    )


def _filter_disliked_ingredients(
    candidates: list[dict],
    disliked_ingredients: list[str],
    graph_rows: list[dict],
) -> list[dict]:
    """Hard-filter allergens across graph, catalog and extracted ingredients."""
    if not disliked_ingredients:
        return candidates

    graph_ingredients: dict[str, list[str]] = {}
    for ingredient in graph_rows:
        product_id = str(ingredient.get("product_id") or "")
        if not product_id:
            continue
        graph_ingredients.setdefault(product_id, []).extend([
            str(ingredient.get("ingredient_name_zh") or ""),
            str(ingredient.get("ingredient_name_en") or ""),
        ])

    fragrance_aliases = (
        "香料", "香精", "fragrance", "parfum", "aroma", "essentialoil", "精油",
        "lavender", "lavandula", "薰衣草", "linalool", "limonene", "geraniol",
        "citronellol", "eugenol",
    )
    allergen_aliases = {
        "香料": fragrance_aliases,
        "香精": fragrance_aliases,
        "酒精": ("酒精", "變性酒精", "alcoholdenat", "denaturedalcohol", "ethanol", "ethylalcohol"),
    }
    disliked_terms: list[str] = []
    for value in disliked_ingredients:
        normalized_value = _normalized_ingredient_text([value])
        if not normalized_value:
            continue
        aliases = allergen_aliases.get(normalized_value, (value,))
        disliked_terms.extend(
            term
            for term in (_normalized_ingredient_text([alias]) for alias in aliases)
            if term and term not in disliked_terms
        )
    safe_candidates = []
    for product in candidates:
        ingredient_values = (
            graph_ingredients.get(str(product.get("id") or ""), [])
            + list(product.get("ingredients_list") or [])
            + list(product.get("key_ingredients") or [])
            + list(product.get("extracted_ingredients") or [])
        )
        ingredient_text = _normalized_ingredient_text(ingredient_values)
        # 有明確過敏條件時，成分未知的商品不可冒充安全。
        if not ingredient_text:
            continue
        if any(term in ingredient_text for term in disliked_terms):
            continue
        safe_candidates.append(product)
    return safe_candidates


def _warned_product_ids(warnings: list, graph_rows: list[dict]) -> set[str]:
    """Map ingredient warnings back to products instead of penalizing every result."""
    warned_ids: set[str] = set()
    warning_text = " ".join(str(value or "") for value in warnings).casefold()
    if not warning_text:
        return warned_ids
    for ingredient in graph_rows:
        product_id = str(ingredient.get("product_id") or "")
        names = (
            str(ingredient.get("ingredient_name_zh") or "").casefold(),
            str(ingredient.get("ingredient_name_en") or "").casefold(),
        )
        if product_id and any(name and name in warning_text for name in names):
            warned_ids.add(product_id)
    return warned_ids


def _apply_personalization_scores(
    candidates: list,
    context: dict,
    warned_ids: set,
    explicit_brands: list = None,
) -> bool:
    """Add a secondary score without weakening safety/budget hard ordering."""
    normalized = _normalize_personalization_context(context)
    if not normalized.get("enabled") or not candidates:
        return False

    prices = []
    for product in candidates:
        try:
            price = float(product.get("price"))
            if price >= 0:
                prices.append(price)
        except (TypeError, ValueError):
            pass
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None
    max_hits = max((int(p.get("concern_hits", 0) or 0) for p in candidates), default=0)

    preferred_brands = set(normalized["preferred_brands"])
    preferred_brands.update(
        str(item).strip().casefold()
        for item in (explicit_brands or [])
        if str(item).strip()
    )

    for product in candidates:
        try:
            price = float(product.get("price"))
        except (TypeError, ValueError):
            price = None
        if price is None or min_price is None or max_price is None:
            price_value = 0.5
        elif max_price == min_price:
            price_value = 0.5
        else:
            price_value = 1.0 - ((price - min_price) / (max_price - min_price))

        rating = product.get("review_score", product.get("rating"))
        review_score = _safe_score(rating, default=0.5)
        if max_hits > 0:
            ingredient_match = min(
                float(product.get("concern_hits", 0) or 0) / max_hits,
                1.0,
            )
        else:
            ingredient_match = _safe_score(product.get("score"), default=0.5)

        brand = str(product.get("brand") or "").strip().casefold()
        if brand and brand in normalized["avoided_brands"]:
            brand_score = 0.0
        elif preferred_brands:
            brand_score = 1.0 if brand in preferred_brands else 0.0
        else:
            brand_score = 0.5

        components = {
            "safety_score": 0.0 if product.get("id") in warned_ids else 1.0,
            "price_value_score": max(0.0, min(price_value, 1.0)),
            "review_score": review_score,
            "ingredient_match_score": ingredient_match,
            "brand_score": brand_score,
        }
        score = sum(
            normalized["weights"][key] * components[key]
            for key in _RANKING_WEIGHT_KEYS
        )
        product["personalization_score"] = round(score, 4)
        product["personalization_customer_type"] = normalized["customer_type"]

    return True


# ──────────────────────────────────────────
# Step 1：Neo4j 向量搜尋
# ──────────────────────────────────────────

def product_search(
    query: str,
    category: str = None,
    top_k: int = 120,
    partner_only: bool = False,
    require_trend_evidence: bool = False,
) -> list:
    """
    商品搜尋。

    有明確品類時：
    Product.category 命中，或泛用分類商品的名稱含品類線索即可進候選池，
    再於完整合格集合中計算向量相似度。

    沒有明確品類時：
    維持原本 product_vector_index 全庫向量搜尋。

    partner_only=True 時：
    只搜尋三友合作商品。既有 is_partner=true 與 source 為
    tomods／tomods_official 都視為三友商品；預設 False，避免影響其他流程。
    """
    embedding_started = time.perf_counter()
    vec = get_query_embedding(query)
    observe_pipeline_stage(
        "product",
        "embedding_encode",
        embedding_started,
    )

    target_cats = None
    if category and category != "不明":
        target_cats = CATEGORY_MAP.get(category, [category])
        if not target_cats:
            target_cats = [category]
    category_keywords = _CATEGORY_NAME_KEYWORDS.get(category, [])
    product_type = _PRODUCT_TYPE_BY_CATEGORY.get(category or "")

    requested_top_k = max(1, int(top_k))
    index_top_k = min(1000, max(200, requested_top_k * int(os.getenv("PRODUCT_VECTOR_PREFETCH_MULTIPLIER", "10"))))
    driver = get_driver()

    vector_query_started = time.perf_counter()
    with driver.session() as session:
        if target_cats:
            result = session.run("""
                MATCH (node:Product)
                WHERE (
                    node.product_type = $product_type
                    OR node.category IN $target_cats
                    OR coalesce(node.category_normalized, '') IN $target_cats
                    OR any(
                        keyword IN $category_keywords
                        WHERE toLower(coalesce(node.name, ''))
                            CONTAINS toLower(keyword)
                           OR toLower(coalesce(node.name_zh, ''))
                            CONTAINS toLower(keyword)
                    )
                )
                  AND ($product_type IS NULL OR coalesce(node.product_type, '') = '' OR node.product_type = $product_type)
                  AND node.embedding IS NOT NULL
                  AND coalesce(node.is_active, true) = true
                  AND (
                      $require_trend_evidence = false
                      OR size(coalesce(node.trend_evidence, [])) > 0
                      OR coalesce(node.evidence_count, 0) > 0
                  )
                  AND (
                      $partner_only = false
                      OR coalesce(node.is_partner, false) = true
                      OR toLower(trim(coalesce(node.source, ''))) IN ['tomods', 'tomods_official']
                  )
                  AND coalesce(
                      node.recommendable,
                      CASE WHEN node.source_type = 'admin_approved' THEN false ELSE true END
                  ) = true
                WITH
                    node,
                    vector.similarity.cosine(
                        node.embedding,
                        $embedding
                    ) AS score
                RETURN
                    elementId(node)                  AS id,
                    node.name                        AS name,
                    node.name_zh                    AS name_zh,
                    node.brand                       AS brand,
                    node.category                    AS category,
                    coalesce(node.product_type, '')  AS product_type,
                    node.price                       AS price,
                    node.source                      AS source,
                    CASE
                        WHEN coalesce(node.is_partner, false) = true
                          OR toLower(trim(coalesce(node.source, ''))) IN ['tomods', 'tomods_official']
                        THEN true ELSE false
                    END AS is_partner,
                    node.ingredients_list AS ingredients_list,
                    coalesce(node.key_ingredients, []) AS key_ingredients,
                    coalesce(node.description, '') AS description,
                    coalesce(node.concerns, [])         AS concerns,
                    coalesce(node.functional_categories, []) AS functional_categories,
                    coalesce(node.common_names, [])     AS common_names,
                    CASE
                        WHEN trim(coalesce(node.source_url, '')) <> ''
                        THEN [node.source_url] + coalesce(node.source_urls, [])
                        ELSE coalesce(node.source_urls, [])
                    END AS source_urls,
                    coalesce(node.cohort, '')           AS cohort,
                    coalesce(node.trend_evidence, [])   AS trend_evidence,
                    coalesce(node.popularity_score, 0.0) AS popularity_score,
                    coalesce(node.evidence_count, 0)    AS evidence_count,
                    coalesce(node.review_score, node.rating, 0.5) AS review_score,
                    score
                ORDER BY
                    CASE WHEN $require_trend_evidence
                         THEN coalesce(node.popularity_score, 0.0)
                         ELSE score
                    END DESC,
                    score DESC
                LIMIT $top_k
            """,
                target_cats=target_cats,
                product_type=product_type,
                category_keywords=category_keywords,
                embedding=vec,
                top_k=index_top_k,
                partner_only=partner_only,
                require_trend_evidence=require_trend_evidence,
            )
        else:
            # Regression guard: WHERE $target_cats IS NULL remains grouped below.
            result = session.run("""
                CYPHER 25
                MATCH (node:Product)
                SEARCH node IN (
                    VECTOR INDEX product_vector_index
                    FOR $embedding
                    LIMIT $index_top_k
                ) SCORE AS score
                WHERE (
                    $target_cats IS NULL
                    OR node.category IN $target_cats
                    OR coalesce(node.category_normalized, '') IN $target_cats
                    OR any(
                        keyword IN $category_keywords
                        WHERE toLower(coalesce(node.name, '')) CONTAINS toLower(keyword)
                           OR toLower(coalesce(node.name_zh, '')) CONTAINS toLower(keyword)
                    )
                )
                  AND coalesce(node.is_active, true) = true
                  AND (
                      $require_trend_evidence = false
                      OR size(coalesce(node.trend_evidence, [])) > 0
                      OR coalesce(node.evidence_count, 0) > 0
                  )
                  AND (
                      $partner_only = false
                      OR coalesce(node.is_partner, false) = true
                      OR toLower(trim(coalesce(node.source, ''))) IN ['tomods', 'tomods_official']
                  )
                  AND coalesce(
                      node.recommendable,
                      CASE WHEN node.source_type = 'admin_approved' THEN false ELSE true END
                  ) = true
                RETURN
                    elementId(node)                  AS id,
                    node.name                        AS name,
                    node.name_zh                    AS name_zh,
                    node.brand                       AS brand,
                    node.category                    AS category,
                    coalesce(node.product_type, '')  AS product_type,
                    node.price                       AS price,
                    node.source                      AS source,
                    CASE
                        WHEN coalesce(node.is_partner, false) = true
                          OR toLower(trim(coalesce(node.source, ''))) IN ['tomods', 'tomods_official']
                        THEN true ELSE false
                    END AS is_partner,
                    node.ingredients_list AS ingredients_list,
                    coalesce(node.key_ingredients, []) AS key_ingredients,
                    coalesce(node.description, '') AS description,
                    coalesce(node.concerns, [])         AS concerns,
                    coalesce(node.functional_categories, []) AS functional_categories,
                    coalesce(node.common_names, [])     AS common_names,
                    CASE
                        WHEN trim(coalesce(node.source_url, '')) <> ''
                        THEN [node.source_url] + coalesce(node.source_urls, [])
                        ELSE coalesce(node.source_urls, [])
                    END AS source_urls,
                    coalesce(node.cohort, '')           AS cohort,
                    coalesce(node.trend_evidence, [])   AS trend_evidence,
                    coalesce(node.popularity_score, 0.0) AS popularity_score,
                    coalesce(node.evidence_count, 0)    AS evidence_count,
                    coalesce(node.review_score, node.rating, 0.5) AS review_score,
                    score
                ORDER BY
                    CASE WHEN $require_trend_evidence
                         THEN coalesce(node.popularity_score, 0.0)
                         ELSE score
                    END DESC,
                    score DESC
                LIMIT $top_k
            """,
                embedding=vec,
                top_k=top_k,
                index_top_k=index_top_k,
                target_cats=target_cats,
                category_keywords=category_keywords,
                partner_only=partner_only,
                require_trend_evidence=require_trend_evidence,
            )

        candidates = _normalize_catalog_prices([dict(r) for r in result])
    observe_pipeline_stage(
        "product",
        "vector_candidate_query",
        vector_query_started,
    )

    return candidates


def partner_product_search(query: str, top_k: int = 60) -> list:
    """Search the partner catalog itself, using the database is_partner flag.

    This is separate from product_search: its legacy partner_only option also
    accepts source labels, and the shared vector index limits before filtering.
    """
    embedding_started = time.perf_counter()
    vec = get_query_embedding(query)
    observe_pipeline_stage("product", "embedding_encode", embedding_started)

    vector_query_started = time.perf_counter()
    with get_driver().session() as session:
        result = session.run("""
            MATCH (node:Product)
            WHERE coalesce(node.is_partner, false) = true
              AND node.embedding IS NOT NULL
              AND coalesce(node.is_active, true) = true
              AND coalesce(
                  node.recommendable,
                  CASE WHEN node.source_type = 'admin_approved' THEN false ELSE true END
              ) = true
            WITH node, vector.similarity.cosine(node.embedding, $embedding) AS score
            RETURN
                elementId(node) AS id,
                node.name AS name,
                node.name_zh AS name_zh,
                node.brand AS brand,
                node.category AS category,
                coalesce(node.category_normalized, '') AS category_normalized,
                node.price AS price,
                node.source AS source,
                coalesce(node.is_partner, false) AS is_partner,
                node.ingredients_list AS ingredients_list,
                coalesce(node.description, '') AS description,
                coalesce(node.concerns, []) AS concerns,
                coalesce(node.functional_categories, []) AS functional_categories,
                coalesce(node.common_names, []) AS common_names,
                coalesce(node.cohort, '') AS cohort,
                score
            ORDER BY score DESC
            LIMIT $top_k
        """, embedding=vec, top_k=max(1, int(top_k)))
        candidates = _normalize_catalog_prices([dict(row) for row in result])
    observe_pipeline_stage("product", "vector_candidate_query", vector_query_started)
    return candidates


def product_search_by_brands(
    brands: list[str],
    category: str = None,
    limit: int = 100,
) -> list:
    """
    使用者明確指定品牌時，品牌是 hard constraint。

    只查正式 Product catalog，不查 UserProduct。
    brand 欄位或商品名稱只要包含指定品牌即可。
    找不到時回傳空列表，不自動改推其他品牌。
    """
    normalized_brands = [
        str(brand).strip().casefold()
        for brand in (brands or [])
        if str(brand).strip()
    ]

    if not normalized_brands:
        return []

    driver = get_driver()

    with driver.session() as session:
        result = session.run("""
            MATCH (node:Product)
            WHERE any(
                brand IN $brands
                WHERE
                    toLower(coalesce(node.brand, '')) CONTAINS brand
                    OR toLower(coalesce(node.name, '')) CONTAINS brand
            )
              AND coalesce(node.is_active, true) = true
              AND coalesce(
                  node.recommendable,
                  CASE WHEN node.source_type = 'admin_approved' THEN false ELSE true END
              ) = true
            RETURN
                elementId(node)                  AS id,
                node.name                        AS name,
                    node.name_zh                    AS name_zh,
                node.brand                       AS brand,
                node.category                    AS category,
                coalesce(node.product_type, '')  AS product_type,
                node.price                       AS price,
                node.source                      AS source,
                coalesce(node.is_partner, false) AS is_partner,
                node.ingredients_list AS ingredients_list,
                coalesce(node.key_ingredients, []) AS key_ingredients,
                coalesce(node.description, '') AS description,
                coalesce(node.concerns, [])         AS concerns,
                coalesce(node.functional_categories, []) AS functional_categories,
                coalesce(node.common_names, [])     AS common_names,
                CASE
                    WHEN trim(coalesce(node.source_url, '')) <> ''
                    THEN [node.source_url] + coalesce(node.source_urls, [])
                    ELSE coalesce(node.source_urls, [])
                END AS source_urls,
                coalesce(node.cohort, '')           AS cohort,
                coalesce(node.trend_evidence, [])   AS trend_evidence,
                coalesce(node.popularity_score, 0.0) AS popularity_score,
                coalesce(node.evidence_count, 0)    AS evidence_count,
                coalesce(node.review_score, node.rating, 0.5) AS review_score,
                1.0                              AS score
            ORDER BY node.name
            LIMIT $limit
        """, brands=normalized_brands, limit=limit)

        candidates = _normalize_catalog_prices([dict(r) for r in result])

    # 明確指定 category 時仍保留 category hard constraint。
    if category and category != "不明":
        target_cats = CATEGORY_MAP.get(category, [category])
        candidates = [
            product
            for product in candidates
            if _matches_requested_category(product, category)
        ]

    return candidates


def _normalize_product_identity(value: object) -> str:
    """Normalize labels so punctuation and spacing do not change product identity."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def find_product_by_identity(
    query: str,
    brand: str = "",
    product_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Find formal catalogue products whose exact identity occurs in the query."""
    normalized_query = _normalize_product_identity(query)
    normalized_brand = _normalize_product_identity(brand)
    wanted_id = str(product_id or "").strip()
    if not normalized_query and not wanted_id:
        return []

    driver = get_driver()
    with driver.session() as session:
        rows = session.run(
            """
            MATCH (node:Product)
            WHERE coalesce(node.is_active, true) = true
              AND coalesce(
                    node.recommendable,
                    CASE WHEN node.source_type = 'admin_approved' THEN false ELSE true END
                  ) = true
              AND (
                    $brand = ''
                    OR toLower(coalesce(node.brand, '')) CONTAINS $brand
                    OR $brand CONTAINS toLower(coalesce(node.brand, ''))
                  )
            RETURN
              elementId(node) AS id,
              node.name AS name,
                    node.name_zh                    AS name_zh,
              node.brand AS brand,
              node.category AS category,
                coalesce(node.category_normalized, '') AS category_normalized,
              node.price AS price,
              node.currency AS currency,
              node.source AS source,
              coalesce(node.is_partner, false) AS is_partner,
              node.ingredients_list AS ingredients_list,
              coalesce(node.key_ingredients, []) AS key_ingredients,
              coalesce(node.description, '') AS description,
              coalesce(node.concerns, []) AS concerns,
              coalesce(node.functional_categories, []) AS functional_categories,
              coalesce(node.common_names, []) AS common_names,
              coalesce(node.canonical_key, '') AS canonical_key,
              coalesce(node.skin_types, node.suitable_skin_types, []) AS skin_types,
              coalesce(node.usage, node.directions, '') AS usage,
              coalesce(node.warnings, node.precautions, []) AS warnings,
              CASE
                WHEN trim(coalesce(node.source_url, '')) <> ''
                THEN [node.source_url] + coalesce(node.source_urls, [])
                ELSE coalesce(node.source_urls, [])
              END AS source_urls,
              1.0 AS score
            LIMIT 1000
            """,
            brand=normalized_brand,
        )
        candidates = _normalize_catalog_prices([dict(row) for row in rows])

    ranked: list[tuple[int, int, dict]] = []
    for item in candidates:
        if wanted_id and str(item.get("id") or "") == wanted_id:
            ranked.append((3, 10_000, item))
            continue
        labels = [item.get("name"), item.get("canonical_key"), *(item.get("common_names") or [])]
        labels = [
            label
            for label in (_normalize_product_identity(value) for value in labels)
            if len(label) >= 4
        ]
        exact = [label for label in labels if label == normalized_query]
        contained = [label for label in labels if label in normalized_query]
        if exact:
            ranked.append((2, max(map(len, exact)), item))
        elif contained:
            ranked.append((1, max(map(len, contained)), item))

    if not ranked:
        return []
    best_rank = max(rank for rank, _, _ in ranked)
    ranked = [item for item in ranked if item[0] == best_rank]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [item[2] for item in ranked[: max(1, int(limit or 1))]]


# ──────────────────────────────────────────
# Step 2a：Partner 商品 → graph_search
# ──────────────────────────────────────────

def _enrich_partner_products(partner_ids: list, skin_type: str) -> dict:
    """Partner 商品走 Knowledge Graph 六條邊查詢"""
    if not partner_ids:
        return {"ingredients": [], "warnings": [], "risks": [],
                "product_suits": [], "suitable_concerns": [],
                "risk_warns": [], "regulations": [], "conflicts": []}
    try:
        return graph_search(partner_ids, user_skin_type=skin_type)
    except Exception as e:
        _log("graph_search", f"失敗：{e}")
        return {"ingredients": [], "warnings": [], "risks": [],
                "product_suits": [], "suitable_concerns": [],
                "risk_warns": [], "regulations": [], "conflicts": []}


# ──────────────────────────────────────────
# Step 2b：External 商品 → TAIDE 抽取成分
# ──────────────────────────────────────────

def _extract_ingredients_batch_taide(products: list) -> dict[int, list]:
    """Extract ingredients for up to five products in one model call."""
    import json as _json

    results: dict[int, list] = {}
    rows = []
    for index, product in enumerate(products[:5]):
        raw_list = product.get("ingredients_list") or []
        fallback = _clean_ingredients(raw_list)[:5]
        results[index] = fallback
        if raw_list:
            rows.append({
                "id": index,
                "name": product.get("name", ""),
                "ingredients": raw_list[:10],
            })

    mode = os.getenv(
        "PRODUCT_INGREDIENT_EXTRACTOR",
        "deterministic",
    ).strip().lower()
    if mode == "deterministic" or not rows:
        return results

    try:
        text = _terra_client.complete(
            model=TERRA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是化妝品成分分析專家。請為每個商品抽取最多5個成分名稱。"
                        "只輸出 JSON 物件，key 必須是商品 id 字串，value 是成分名稱陣列。"
                        "不要輸出說明文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": _json.dumps(rows, ensure_ascii=False),
                },
            ],
            max_tokens=int(os.getenv("PRODUCT_INGREDIENT_MAX_TOKENS", "300")),
            temperature=0.0,
            timeout_seconds=float(
                os.getenv("PRODUCT_INGREDIENT_TIMEOUT_SECONDS", "30")
            ),
        )
        start = text.find("{")
        if start < 0:
            raise ValueError("TAIDE batch response did not contain a JSON object")
        parsed, _ = _json.JSONDecoder().raw_decode(text[start:])

        for index in range(min(5, len(products))):
            values = parsed.get(str(index), [])
            cleaned = _clean_ingredients(values)[:5]
            if cleaned:
                results[index] = cleaned
    except Exception as error:
        _log("INGREDIENT_BATCH", f"批次抽取失敗，使用既有成分資料：{error}")

    return results


# ──────────────────────────────────────────
# Step 3：A2A → 知識庫 Agent（查法規）
# ──────────────────────────────────────────

def _a2a_knowledge_query(ingredients: list, concerns: list, category: str) -> dict:
    """
    A2A 呼叫知識庫 Agent
    取得 allowed_claims / banned_claims / ingredient_benefits
    """
    try:
        from agents.knowledge.knowledge_agent import run as knowledge_agent_run
        result = knowledge_agent_run(
            user_query=f"這些成分{'、'.join(ingredients[:3])}用於{category}，有什麼需要注意的？",
            is_a2a=True,
            user_skin_type="normal",
        )
        payload = result.get("payload", {})
        return {
            "allowed_claims":      payload.get("allowed_claims", []),
            "banned_claims":       payload.get("banned_claims", []),
            "ingredient_benefits": payload.get("ingredient_benefits", {}),
        }
    except Exception as e:
        _log("A2A", f"知識庫 Agent 查詢失敗（略過）：{e}")
        return {"allowed_claims": [], "banned_claims": [], "ingredient_benefits": {}}


# ──────────────────────────────────────────
# 主流程：run()
# ──────────────────────────────────────────

def _product_series_key(name: str) -> str:
    """
    將同系列的色號／香味／容量 variant 歸到同一主商品。

    例：
    ALLIE 持采亮化UV防曬水凝乳(裸肌香檸茶香)
    ALLIE 持采亮化UV防曬水凝乳(光肌茉桃香)
    -> 同一 series key

    不會移除一般功能名稱，例如：
    B:ample ... -透亮
    B:ample ... -緊緻
    仍會視為不同產品。
    """
    value = str(name or "").strip().lower()

    # 品牌名稱格式差異
    value = re.sub(
        r"mentholatum",
        "",
        value,
        flags=re.I,
    )

    # 同義品類名稱
    value = value.replace(
        "潔面乳",
        "洗面乳",
    )

    # 尾端括號通常是色號、香味、限定款、MINI 等 variant。
    value = re.sub(
        r"[（(][^（）()]{1,50}[）)]\s*$",
        "",
        value,
    )

    # 明確色號標示。
    value = re.sub(
        r"(?:色號|shade)\s*[:#]?\s*[0-9a-z_-]+\s*$",
        "",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"#\s*[0-9a-z_-]+\s*$",
        "",
        value,
        flags=re.I,
    )

    # 容量 / 包裝數量不應形成不同主商品。
    value = re.sub(
        r"\d+(?:\.\d+)?\s*(ml|g|枚|片|入|盒)\s*$",
        "",
        value,
        flags=re.I,
    )

    # 最後只留可比較字元。
    value = re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+",
        "",
        value,
    )

    return value



def _trend_evidence_count(product: dict) -> int:
    try:
        return max(0, int(product.get("evidence_count") or 0))
    except (TypeError, ValueError):
        return len(product.get("trend_evidence") or [])


def _has_trend_evidence(product: dict) -> bool:
    return bool(product.get("trend_evidence")) or _trend_evidence_count(product) > 0


def _product_rank_key(
    product: dict,
    warned_ids: set,
    budget_val: float = None,
    personalization_applied: bool = False,
    trend_first: bool = False,
) -> tuple:
    """Rank commercial partnership only after user relevance signals."""
    base = (
        1 if product.get("id") in warned_ids else 0,
        1 if (
            budget_val
            and (product.get("price") or 0) > budget_val
        ) else 0,
        0 if _product_data_quality(product) == "verified" else 1,
    )
    if trend_first:
        base += (
            0 if _has_trend_evidence(product) else 1,
            -product.get("popularity_score", 0),
            -_trend_evidence_count(product),
        )
    base += (-product.get("concern_hits", 0),)
    if personalization_applied:
        base += (-product.get("personalization_score", 0),)
    return base + (
        -product.get("efficacy_score", 0),
        0 if product.get("has_ingredient_evidence") else 1,
        -product.get("popularity_score", 0),
        -product.get("score", 0),
        0 if product.get("is_partner") else 1,
    )


def run(
    intent: IntentParseResult,
    disliked_ingredients: list = None,
    memory_context: str = "",
    preference_context: dict = None,
    excluded_product_ids: list = None,
) -> dict:
    """
    藥妝保養品推薦主流程（含內部 Orchestration）

    決策流程：
    search → 分流(partner/external) → A2A知識庫(有成分才查) → 過濾
    → 排序(警告→預算→partner→分數) → 文案 → 輸出
    """
    disliked_ingredients = disliked_ingredients or []
    excluded_product_ids = {str(product_id) for product_id in (excluded_product_ids or [])}

    query     = ' '.join(intent.query_expansion) if intent.query_expansion else intent.raw_query
    # 品類詞強制注入（intent_parser 的 expansion 不一定帶品類，漏了會撈到隔壁品類）
    _cat = intent.product_category.value
    if _cat and _cat != '不明' and _cat not in query:
        query = f"{query} {_cat} {_cat}"
        _log("STEP1", f"品類詞注入：{_cat}")
    category  = intent.product_category.value if intent.product_category.value != "不明" else None
    skin_type = intent.skin_type.value if intent.skin_type.value != "unknown" else None
    concerns  = intent.concern or []
    budget_val = _parse_budget(getattr(intent, 'budget_range', None))

    trend_request = is_trending_product_query(intent)
    candidate_pool = int(os.getenv("PRODUCT_CANDIDATE_POOL_SIZE", "40"))
    if trend_request:
        candidate_pool = max(
            candidate_pool,
            int(os.getenv("PRODUCT_TREND_CANDIDATE_POOL_SIZE", "200")),
        )

    _log("STEP1", f"向量搜尋：query={query[:30]}, category={category}, budget={budget_val}")

    # ── Step 1：向量搜尋 ──
    stage_started = time.perf_counter()
    explicit_brands = [
        str(brand).strip()
        for brand in (getattr(intent, "brand_preference", None) or [])
        if str(brand).strip()
    ]

    if explicit_brands:
        _log(
            "STEP1",
            f"品牌 hard constraint：{explicit_brands}",
        )
        candidates = product_search_by_brands(
            explicit_brands,
            category=category,
            limit=100,
        )
    else:
        candidates = product_search(
            query,
            category=category,
            top_k=candidate_pool,
            require_trend_evidence=trend_request,
        )
    candidates = filter_unseen_products(candidates, excluded_product_ids)
    candidates = [
        product
        for product in candidates
        if _is_display_ready_product(product)
        and _matches_requested_category(product, category)
    ]
    _log("STEP1", f"完整資料與品類硬篩選後 {len(candidates)} 筆")
    observe_pipeline_stage(
        "product",
        "vector_search",
        stage_started,
    )

    # 知名／熱門品牌要求先使用本地已審核且有熱門度證據的商品；
    # 本地沒有可信證據時才啟動 Web Search，避免把語意相似誤當成知名。
    if trend_request:
        local_trend_candidates = [
            product for product in candidates if _has_trend_evidence(product)
        ]
        if not local_trend_candidates:
            from tools.search.product_web_search import search_named_product

            web_result = search_named_product(
                intent.raw_query,
                skin_type=skin_type or "unknown",
                concerns=concerns,
                category=category,
                brand_hint="、".join(explicit_brands),
                max_results=3,
                requested_fields=[
                    "brand", "name", "price", "category",
                    "description", "source_url",
                ],
                fallback_reason="insufficient_local_trend_evidence",
            )
            web_products = list(web_result.get("products") or [])
            # budget_range 只會在使用者明確提出預算（含上一輪沿用）時存在；
            # Web 結果價格未知時不可冒充為符合預算。
            hard_budget_request = bool(budget_val)
            if hard_budget_request:
                web_products = [
                    product
                    for product in web_products
                    if 0 < float(product.get("price") or 0) <= budget_val
                ]
            if web_products:
                web_result["products"] = web_products
                web_result["fallback_reason"] = "insufficient_local_trend_evidence"
                web_result["message"] = (
                    "本地商品庫沒有足夠的知名度證據，以下改用可追溯的網路來源補充；"
                    "結果不會自動寫入正式商品庫。"
                )
                _log(
                    "WEB_FALLBACK",
                    f"本地缺少熱門證據，Web Search 補入 {len(web_products)} 筆",
                )
                return web_result

    # 品類已在 retrieval 層使用 category hard constraint，
    # 不再以商品名稱做第二次猜測式過濾。

    if not candidates:
        if trend_request:
            return {
                "products": [],
                "graph_summary": "",
                "warnings": [],
                "total_candidates": 0,
                "message": (
                    "目前沒有同時通過資料完整性、品類／預算條件，且具可追溯"
                    "熱門度證據的商品。我不會把搜尋排名當成熱門證明。"
                    "你可以指定品牌讓我查正式商品頁，或改成依成分與膚況推薦。"
                ),
            }
        if explicit_brands:
            from tools.search.product_web_search import search_named_product

            web_result = search_named_product(
                intent.raw_query,
                skin_type=skin_type or "unknown",
                concerns=concerns,
                category=category,
                brand_hint="、".join(explicit_brands),
                max_results=3,
            )
            if web_result.get("products"):
                _log(
                    "WEB_FALLBACK",
                    "指定品牌不在本地商品庫，改用網路來源",
                )
                return web_result

            brand_text = "、".join(explicit_brands)
            return {
                "products": [],
                "graph_summary": "",
                "warnings": [],
                "total_candidates": 0,
                "message": (
                    f"目前商品資料庫中找不到符合「{brand_text}」品牌條件的商品。"
                    "我不會自動改推其他品牌。"
                    "如果你願意，我可以幫你取消品牌限制，改找其他符合需求的商品。"
                ),
            }

        return {
            "products": [],
            "graph_summary": "",
            "warnings": [],
            "total_candidates": 0,
            "message": "找不到符合條件的保養品，請試試調整搜尋條件。",
        }

    # ── Step 2：分流 partner / external ──
    partner_products  = [p for p in candidates if p.get('is_partner')]
    external_products = [p for p in candidates if not p.get('is_partner')]
    partner_ids       = [p['id'] for p in partner_products]
    all_ids           = [p['id'] for p in candidates]

    _log("STEP2", f"partner={len(partner_products)} 筆 / external={len(external_products)} 筆")

    # Step 2a：Partner 商品走 graph_search
    stage_started = time.perf_counter()
    graph_result = _enrich_partner_products(
        partner_ids or all_ids[:10],
        skin_type,
    )
    observe_pipeline_stage(
        "product",
        "neo4j_enrichment",
        stage_started,
    )

    # Step 2b：External 商品成分一次批次處理，避免最多 5 次串行模型呼叫。
    stage_started = time.perf_counter()
    batched_ingredients = _extract_ingredients_batch_taide(
        external_products[:5]
    )
    observe_pipeline_stage(
        "product",
        "ingredient_batch",
        stage_started,
    )
    for index, product in enumerate(external_products[:5]):
        product["extracted_ingredients"] = batched_ingredients.get(
            index,
            [],
        )

    # ── Step 3：A2A → 知識庫 Agent（有成分才查）──
    all_ingredients = list({
        i['ingredient_name_zh']
        for i in graph_result.get('ingredients', [])
        if i.get('ingredient_name_zh')
    })
    for p in external_products[:5]:
        all_ingredients.extend(p.get('extracted_ingredients', []))
    all_ingredients = list(set(all_ingredients))[:8]

    # A2A 統一由 tools/agent/product_knowledge_orchestrator 包裝層負責，
    # 此處不再直接呼叫知識庫，避免同一輪推薦重複 A2A
    _log("STEP3", f"成分彙整完成（{len(all_ingredients)} 項），A2A 交由包裝層處理")
    knowledge_result = {"allowed_claims": [], "banned_claims": [], "ingredient_benefits": {}}

    # ── Step 4：過濾禁忌成分 ──
    global_warnings = graph_result.get('warnings', [])

    if disliked_ingredients:
        candidates = _filter_disliked_ingredients(
            candidates,
            disliked_ingredients,
            graph_result.get('ingredients', []),
        )
        _log("STEP4", f"過濾禁忌成分後剩 {len(candidates)} 筆")
        if not candidates:
            return {
                "products": [],
                "graph_summary": "",
                "warnings": global_warnings,
                "total_candidates": 0,
                "message": {
                    "main": "已套用你的過敏／禁忌成分，但目前沒有可安全核對的商品。",
                    "sub": "我不會用成分不完整的商品冒充符合；可改品類或指定商品再查。",
                },
            }

    # ── Step 5：排序（安全/預算 → 需求 → 個人化/語意 → partner 同分優先）──
    if trend_request:
        evidenced_candidates = [p for p in candidates if _has_trend_evidence(p)]
        if not evidenced_candidates:
            return {
                "products": [],
                "graph_summary": "",
                "warnings": global_warnings,
                "total_candidates": len(candidates),
                "message": (
                    "目前資料庫沒有足夠的熱門度證據，因此不會把一般相似商品"
                    "標示為熱門。你可以改查指定品牌或商品，我再補充網路來源。"
                ),
            }
        candidates = evidenced_candidates
        _log("STEP4", f"熱門證據篩選後剩 {len(candidates)} 筆")

    warned_ids = _warned_product_ids(
        global_warnings,
        graph_result.get('ingredients', []),
    )

    if budget_val:
        hard_budget = bool(re.search(
            r"(以下|以內|不超過|最多|低於|少於|under|below|up\s+to)",
            getattr(intent, "raw_query", "") or "",
            re.IGNORECASE,
        ))
        if hard_budget:
            candidates = [
                product
                for product in candidates
                if 0 < (product.get("price") or 0) <= budget_val
            ]
            _log("STEP5", f"預算硬條件 {budget_val} 元內，剩 {len(candidates)} 筆")
            if not candidates:
                return {
                    "products": [],
                    "graph_summary": "",
                    "warnings": global_warnings,
                    "total_candidates": 0,
                    "message": {
                        "main": f"目前找不到價格在 {budget_val:g} 元以內且符合條件的商品。",
                        "sub": "你可以提高預算、放寬品類，或指定品牌再試一次。",
                    },
                }
        else:
            over_budget = sum(1 for p in candidates if (p.get('price') or 0) > budget_val)
            _log("STEP5", f"預算 {budget_val} 元，{over_budget} 筆超預算往後排")

    # concern 匹配：商品名稱含訴求關鍵字 + graph suitable_concerns 命中
    CONCERN_SYNONYM = {
        '保濕': ['保濕', '補水', '鎖水', '水潤'],
        '抗痘': ['抗痘', '痘', '粉刺', '痘痘'],
        '控油': ['控油', '去油', '油光', '出油'],
        '美白': ['美白', '亮白', '提亮', '淡斑'],
        '舒敏': ['舒敏', '舒緩', '低敏', '溫和'],
        '修護': ['修護', '修復', '屏障'],
        '抗老': ['抗老', '緊緻', '撫紋', '彈力'],
    }
    suit_map = {}
    for sc in graph_result.get('suitable_concerns', []):
        pid = sc.get('product_id')
        if pid:
            suit_map.setdefault(pid, set()).add(str(sc.get('concern_name_zh', '')))

    EFFICACY_INGREDIENT_TERMS = {
        '保濕': ('glycerin', 'hyaluronic', 'sodium hyaluronate', 'ceramide', 'panthenol', 'squalane', 'beta-glucan', '尿囊素'),
        '修護': ('ceramide', 'panthenol', 'madecassoside', 'centella', 'squalane', '膽固醇', '神經醯胺'),
        '美白': ('niacinamide', 'ascorbic', 'vitamin c', 'tranexamic', 'alpha-arbutin', 'arbutin', 'kojic', '菸鹼醯胺', '傳明酸'),
        '抗痘': ('salicylic', 'azelaic', 'mandelic', 'niacinamide', '水楊酸', '壬二酸', '杏仁酸'),
        '控油': ('niacinamide', 'zinc pca', 'salicylic', '菸鹼醯胺', '水楊酸'),
        '舒敏': ('panthenol', 'madecassoside', 'centella', 'allantoin', 'beta-glucan', '積雪草', '尿囊素'),
        '抗老': ('retinol', 'retinal', 'peptide', 'adenosine', 'bakuchiol', '視黃醇', '胜肽'),
    }
    FUNCTION_BY_CONCERN = {
        '保濕': {'hydration_repair'}, '修護': {'hydration_repair'},
        '美白': {'brightening'}, '抗痘': {'acne_pores'}, '控油': {'acne_pores'},
        '舒敏': {'sensitive_soothing'}, '抗老': {'anti_aging'},
    }

    def _efficacy_evidence(p):
        score = 0
        reasons = []
        name = str(p.get('name') or '')
        structured_concerns = {str(value) for value in (p.get('concerns') or [])}
        functions = {str(value) for value in (p.get('functional_categories') or [])}
        ingredients = [
                str(value)
                for value in (
                    list(p.get('ingredients_list') or [])
                    + list(p.get('key_ingredients') or [])
                )
            ]
        ingredient_text = ' '.join(ingredients).casefold()
        suits = suit_map.get(p['id'], set())
        for concern in concerns:
            keywords = CONCERN_SYNONYM.get(concern, [concern])
            ingredient_hits = [
                term for term in EFFICACY_INGREDIENT_TERMS.get(concern, ())
                if term.casefold() in ingredient_text
            ]
            if ingredient_hits:
                score += 6
                reasons.append(_ingredient_support_reason(ingredient_hits[0], concern))
            if any(any(keyword in value for keyword in keywords) for value in suits):
                score += 5
                reasons.append(f"知識圖譜標示符合{concern}需求")
            if (
                any(any(keyword in value for keyword in keywords) for value in structured_concerns)
                or bool(functions & FUNCTION_BY_CONCERN.get(concern, set()))
            ):
                score += 3
                reasons.append(f"審核資料標示為{concern}用途")
            if any(keyword in name for keyword in keywords):
                score += 1
        return score, list(dict.fromkeys(reasons))[:3]

    for p in candidates:
        efficacy_score, efficacy_reasons = _efficacy_evidence(p)
        p['efficacy_score'] = efficacy_score
        p['efficacy_reasons'] = efficacy_reasons
        p['has_ingredient_evidence'] = bool(
            _clean_ingredients(
                list(p.get('ingredients_list') or [])
                + list(p.get('key_ingredients') or [])
            )
        )
        p['concern_hits'] = efficacy_score

    personalization_applied = _apply_personalization_scores(
        candidates,
        preference_context,
        warned_ids,
        explicit_brands=getattr(intent, "brand_preference", None),
    )

    if personalization_applied:
        candidates.sort(
            key=lambda p: _product_rank_key(
                p,
                warned_ids,
                budget_val,
                personalization_applied=True,
                trend_first=trend_request,
            )
        )
        _log(
            "STEP5",
            "已套用消費人格排序："
            + str((preference_context or {}).get("customer_type_label") or ""),
        )
    else:
        candidates.sort(
            key=lambda p: _product_rank_key(
                p,
                warned_ids,
                budget_val,
                trend_first=trend_request,
            )
        )

    # ── Step 5.5：近似商品名稱去重 ──
    #
    # 同系列不同資料來源常會有：
    # Mentholatum曼秀雷敦 / 曼秀雷敦
    # 洗面乳 / 潔面乳
    # 容量尾碼
    #
    # 避免前五名被同一商品系列佔滿。
    # 同系列商品依 series key 去重。
    # candidates 已完成排序，因此保留排序最高的 variant。

    deduped = []
    seen_names = set()

    for product in candidates:
        normalized_name = _product_series_key(
            product.get("name", "")
        )

        if (
            normalized_name
            and normalized_name in seen_names
        ):
            continue

        seen_names.add(
            normalized_name
        )
        deduped.append(
            product
        )

    if len(deduped) != len(candidates):
        _log(
            "STEP5.5",
            f"近似商品去重：{len(candidates)}→{len(deduped)}",
        )

    candidates = deduped


    # ── Step 6：生成合規文案 ──
    output_products = []
    for p in candidates[:3]:
        # 成分來源：partner 用 graph_search 結果，external 用 TAIDE 抽取
        ing_for_copy = _dedupe_display_ingredients([i['ingredient_name_zh'] for i in graph_result.get('ingredients', []) if i.get('product_id') == p['id'] and i.get('ingredient_name_zh')] + list(p.get('ingredients_list') or []) + list(p.get('key_ingredients') or []))

        # 文案改由批次生成（迴圈後一次 TAIDE 呼叫），這裡先填 fallback
        ing_str     = "、".join(ing_for_copy[:3]) if ing_for_copy else "多種保養成分"
        concern_str = "、".join(concerns[:2]) if concerns else "日常保養"
        copy_text = str(p.get("description") or "").strip()

        name_text = p.get('name', '')

        matched_claim_terms = []
        for concern in concerns:
            for keyword in CONCERN_SYNONYM.get(concern, [concern]):
                if keyword and keyword in name_text:
                    matched_claim_terms.append(keyword)

        # 去重但保留順序
        matched_claim_terms = list(
            dict.fromkeys(matched_claim_terms)
        )[:4]

        output_products.append({
            'id':          p['id'],
            'name':        (str(p.get('name_zh') or '').strip() or name_text),
            'brand':       str(p.get('brand') or '').strip() or '未標示',
            'category':    p.get('category', ''),
            'price':       p.get('price', 0),
            'copy':        copy_text,
            'is_partner':  p.get('is_partner', False),
            'score':       round(p.get('score', 0), 3),
            'ingredients': ing_for_copy,
            'concern_hits': p.get('concern_hits', 0),
            'matched_claim_terms': matched_claim_terms,
            'over_budget': bool(
                budget_val
                and (p.get('price') or 0) > budget_val
            ),
            'personalization_score': p.get('personalization_score'),
            'efficacy_score': p.get('efficacy_score', 0),
            'efficacy_reasons': p.get('efficacy_reasons', []),
            'has_ingredient_evidence': p.get('has_ingredient_evidence', False),
            "data_quality": _product_data_quality(p),
            'popularity_score': p.get('popularity_score', 0.0),
            'trending': (p.get('trend_evidence') or [''])[0],
            'sources': [
                {'url': url}
                for url in (p.get('source_urls') or [])[:1]
                if str(url).startswith(('http://', 'https://'))
            ],
        })

    # 一次 TAIDE 呼叫批次生成文案與排序理由。
    # 不生成商品間比較，避免回覆出現「和上一款相比」。
    stage_started = time.perf_counter()
    output_products = _batch_generate_rank_content(
        output_products,
        skin_type or 'unknown',
        concerns,
        global_warnings,
        memory_context=memory_context,
    )
    observe_pipeline_stage(
        "product",
        "batch_copy_generation",
        stage_started,
    )

    _log("OUTPUT", f"輸出 {len(output_products)} 筆商品")

    return {
        "products":         output_products,
        "terra_intro": generate_product_intro(intent.raw_query, category).get("intro", ""),
        "graph_summary":    format_graph_result(graph_result),
        "warnings":         global_warnings,
        "knowledge_result": knowledge_result,
        "total_candidates": len(candidates),
        "personalization_applied": personalization_applied,
        "customer_type": (
            (preference_context or {}).get("customer_type")
            if personalization_applied
            else None
        ),
    }
# ──────────────────────────────────────────
# 批次生成：排序理由 + 商品比較 + 文案（一次 TAIDE 呼叫）
# ──────────────────────────────────────────

def _batch_generate_rank_content(
    products: list,
    skin_type: str,
    concerns: list,
    warnings: list,
    memory_context: str = "",
) -> list:
    """一次 TAIDE 呼叫為全部商品生成 copy；失敗填 fallback 文案。"""
    import json as _json

    skin_map = {"oily": "油性肌", "dry": "乾性肌", "sensitive": "敏感肌",
                "combination": "混合肌", "normal": "中性肌"}
    skin_label = skin_map.get(
        skin_type,
        "膚質尚未確認",
    )
    concern_str = "、".join(concerns[:3]) if concerns else "日常保養"
    displayable_concerns = {
        "保濕", "緊緻", "抗皺", "亮白", "透亮", "逆齡", "舒緩",
        "舒敏", "抗痘", "粉刺", "控油", "毛孔", "美白", "淡斑", "修護",
    }
    primary_concern = next(
        (
            str(value).strip()
            for value in concerns
            if str(value).strip() in displayable_concerns
        ),
        "",
    )

    def _fill_fallback():
        for p in products:
            ingredients = [
                _display_ingredient(str(x).strip())
                for x in (p.get("ingredients") or [])
                if str(x).strip()
            ]

            ing_str = "、".join(ingredients[:3])
            category_label = str(p.get("category") or "保養商品").strip()

            if not p.get("copy"):
                if ing_str:
                    p["copy"] = (
                        f"目前商品資料列出的成分包含{ing_str}；"
                        "建議再查看完整成分標示與使用方式，"
                        "確認是否符合你的需求。"
                    )
                else:
                    category_label = str(p.get("category") or "保養商品").strip()
                    concern_label = primary_concern or concern_str
                    p["copy"] = (
                        f"此商品符合目前的{category_label}與{concern_label}篩選條件；"
                        "可從商品頁核對容量、用法與適用部位。"
                    )

            # 功效證據永遠在熱門度之前；熱門只說明接受度。
            reason_parts = list(p.get("efficacy_reasons") or [])

            if not reason_parts and p.get("concern_hits", 0) > 0:
                if p.get("data_quality") == "verified" or bool(p.get("ingredients")):
                    reason_parts.append(
                        f"與本輪{primary_concern}保養需求有直接匹配，且有資料證據支持"
                        if primary_concern else "與本輪保養需求有直接匹配，且有資料證據支持"
                    )
                else:
                    reason_parts.append(
                        f"商品名稱或類型符合本輪{primary_concern}篩選條件"
                        if primary_concern else "商品名稱或類型符合本輪篩選條件"
                    )

            if not reason_parts:
                name_text = str(p.get("name") or "").strip()
                texture_term = next(
                    (term for term in ("水感", "水凝", "凝乳", "清爽", "輕盈") if term in name_text),
                    "",
                )
                if texture_term:
                    reason_parts.append(
                        f"商品名稱標示「{texture_term}」質地，可作為比較清爽度的具體依據"
                    )
                if category_label and category_label not in {"其他", "臉部保養", "保養商品"}:
                    reason_parts.append(f"商品名稱與類型明確對應本輪{category_label}需求")

            if p.get("is_partner"):
                reason_parts.append(
                    "可取得較完整的商品關聯資料"
                )

            if p.get("over_budget"):
                reason_parts.append(
                    "價格超出目前預算，排序已往後調整"
                )

            if not reason_parts:
                reason_parts.append(
                    "目前缺少足夠成分功效證據，僅列為次要比較候選"
                )

            if p.get("trending"):
                reason_parts.append(f"熱門依據：{p['trending']}")

            p["recommendation_reason"] = "；".join(reason_parts)
            p["rank_reason"] = p["recommendation_reason"]
            p.pop("vs_prev", None)
    if os.getenv("PRODUCT_COPY_USE_LLM", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        _fill_fallback()
        _log(
            "BATCH_COPY_FAST_PATH",
            "使用資料庫證據產生商品文案，跳過額外 TAIDE 呼叫",
        )
        return products

    lines = []
    for i, p in enumerate(products):
        ing = (
            "、".join((p.get("ingredients") or [])[:4])
            or "未標示"
        )

        claim_terms = (
            "、".join(
                p.get("matched_claim_terms") or []
            )
            or "無"
        )

        lines.append(
            "\n".join([
                f"商品{i+1}",
                f"名稱：{p.get('name', '')}",
                f"品牌：{p.get('brand', '') or '未標示'}",
                f"品類：{p.get('category', '')}",
                f"價格：NT${p.get('price', 0)}",
                f"成分資料：{ing}",
                f"商品名稱明確命中的需求詞：{claim_terms}",
                f"需求命中數：{p.get('concern_hits', 0)}",
                f"合作商品：{'是' if p.get('is_partner') else '否'}",
                f"超出預算：{'是' if p.get('over_budget') else '否'}",
            ])
        )

    product_block = "\n\n".join(lines)
    warning_note  = f"\n共同注意事項：{'；'.join(warnings[:2])}" if warnings else ""
    memory_note = (memory_context or "").strip()[:6000]

    system_prompt = (
        "你是台灣藥妝文案專家，熟悉衛福部化妝品法規。\n"
        "禁用詞：治療、醫治、消炎、殺菌、抗菌、醫師推薦、消除痘痘。\n"
        f"使用者膚質：{skin_label}，訴求：{concern_str}。{warning_note}\n\n"
        "以下是這位使用者與本輪問題相關的歷史紀錄，只能在相關時參考；"
        "本輪明確描述優先，不可捏造紀錄，也不可向使用者提到資料庫或記憶系統：\n"
        f"{memory_note or '沒有可用的相關歷史紀錄'}\n\n"
        "若歷史顯示使用者曾不喜歡、使用不適或明確拒絕某商品/成分，"
        "不得把該項寫成正面首選；若曾購買或使用過，可在文案中自然承接。\n\n"
        "為下列每個商品依序輸出 JSON 陣列，每筆格式：\n"
        '{"copy": "推薦文案，50-70字，親切自然，只能根據已提供的商品資料撰寫，以句號或驚嘆號完整收尾"}\n'
        "文案必須使用一般消費者容易理解的說法，不可出現「配方資料已可核對」、"
        "「本輪需求」或「候選」等內部作業用語。\n"
        "有提供成分時，只能說商品資料『有列出』哪些成分，不可暗示完整配方已驗證；"
        "成分資料為『未標示』時，請直接說資料較不齊全，並提醒查看完整標示與使用方式後再做局部測試。\n"
        "禁止輸出商品間比較，也禁止出現「和上一款相比」或「與前一名相比」。\n"
        "重要事實限制：\n"
        "1. 不得自行產生、修改或推測價格、折扣、百分比、容量、排名、評分或銷量。\n"
        "2. 不得捏造未提供的成分。\n"
        "3. 不得因商品名稱自行推測成分功效。\n"
        "4. 膚質若是「膚質尚未確認」，不得宣稱商品適合使用者的特定膚質。\n"
        "只輸出完整合法的 JSON 陣列（所有引號閉合、以]結尾），不要任何其他文字。"
    )

    try:
        text = _terra_client.complete(
            model=TERRA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": product_block},
            ],
            max_tokens=int(os.getenv("PRODUCT_COPY_MAX_TOKENS", "600")),
            temperature=0.3,
            timeout_seconds=float(os.getenv("PRODUCT_COPY_TIMEOUT_SECONDS", "60")),
        )
        start = text.find('['); end = text.rfind(']') + 1
        try:
            items = _json.loads(text[start:end])
        except Exception:
            import re as _re
            items = []
            for mm in _re.finditer(r'\{[^{}]*\}', text):
                try:
                    items.append(_json.loads(mm.group()))
                except Exception:
                    pass
            if not items:
                raise
        for p, item in zip(products, items):
            if item.get('copy'):
                copy_text = str(item['copy'])
                for marker in (
                    '和上一款相比：', '和上一款相比:',
                    '與上一款相比：', '與上一款相比:',
                    '與前一名相比：', '與前一名相比:',
                ):
                    if marker in copy_text:
                        copy_text = copy_text.split(marker, 1)[0].rstrip()
                        break
                copy_text = re.sub(r"[|｜]+", "，", copy_text)
                copy_text = re.sub(
                    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF]",
                    "",
                    copy_text,
                )
                p['copy'] = copy_text[:120]
            p.pop('vs_prev', None)
        _fill_fallback()   # TAIDE 少給幾筆時補齊

        _log("BATCH_COPY", f"批次生成成功 {len(items)} 筆（1 次 TAIDE 呼叫）")
    except Exception as e:
        _log("BATCH_COPY", f"批次生成失敗，用 fallback：{e}")
        _fill_fallback()
    return products


if __name__ == '__main__':
    from schemas.intent import (
        IntentParseResult, IntentType, SkinType,
        ProductCategory, TexturePreference
    )

    print("=== 測試 1：乾肌找保濕面膜（預算300）===")
    r1 = run(IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.DRY,
        concern=['保濕'],
        product_category=ProductCategory.MASK,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['保濕', '補水', '鎖水', '乾燥', '面膜'],
        raw_query='乾肌找保濕面膜，預算300以內',
        ambiguity_score=0.1,
        budget_range='300',
    ))
    for p in r1['products']:
        tag  = "⭐" if p['is_partner'] else "  "
        over = " 💸超預算" if p.get('over_budget') else ""
        print(f"  {tag}[{p['score']}] {p['name']} | NT${p['price']}{over}")
        print(f"     文案：{p.get('copy','')}")

    print("\n=== 測試 2：敏感肌精華液（過濾維他命C）===")
    r2 = run(IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.SENSITIVE,
        concern=['舒敏', '保濕'],
        product_category=ProductCategory.SERUM,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['敏感肌', '舒緩', '低刺激', '精華液'],
        raw_query='敏感肌找舒緩精華液',
        ambiguity_score=0.1,
    ), disliked_ingredients=['維他命C'])
    for p in r2['products']:
        tag = "⭐" if p['is_partner'] else "  "
        print(f"  {tag}[{p['score']}] {p['name']} | NT${p['price']}")
        print(f"     文案：{p.get('copy','')}")

    print("\n=== 測試 3：油性肌抗痘洗面乳 ===")
    r3 = run(IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.OILY,
        concern=['抗痘', '控油'],
        product_category=ProductCategory.CLEANSER,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['痘痘', '粉刺', '控油', '出油', '洗面乳'],
        raw_query='油性肌抗痘洗面乳',
        ambiguity_score=0.05,
    ))
    for p in r3['products']:
        tag = "⭐" if p['is_partner'] else "  "
        print(f"  {tag}[{p['score']}] {p['name']} | NT${p['price']}")
        print(f"     文案：{p.get('copy','')}")

    if _driver:
        _driver.close()
