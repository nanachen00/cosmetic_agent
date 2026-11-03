"""Human-reviewed product catalogue staging and activation service.

Candidates stay isolated from recommendations until an administrator approves
one. Approval creates its search embedding and idempotently upserts the Product
node into Neo4j; failed imports remain visible and can be retried safely.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from app.clients.embedding_client import EmbeddingClient


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = Path(
    os.getenv(
        "PRODUCT_REVIEW_DATA_DIR",
        str(_PROJECT_ROOT / "data" / "product_review"),
    )
)
_SEED_FILES = (
    _DATA_DIR / "product_catalog_seed_2026.json",
    _DATA_DIR / "product_catalog_seed_historical_2024_2026.json",
    _DATA_DIR / "product_catalog_crawled_candidates.json",
)
_SOURCE_MANIFEST = _DATA_DIR / "product_trend_sources_2024_2026.json"
_BRAND_MARKET_REGISTRY = _DATA_DIR / "brand_market_registry.json"
_PRODUCT_NAME_REGISTRY = _DATA_DIR / "product_name_registry.json"
_DECISIONS_FILE = _DATA_DIR / "product_review_decisions.json"
_VERIFIED_ENRICHMENTS_FILE = _DATA_DIR / "product_verified_enrichments.json"
_WRITE_LOCK = threading.Lock()

REVIEW_STATUSES = {
    "pending_review",
    "approved",
    "needs_enrichment",
    "excluded",
    "duplicate",
}

FUNCTION_LABELS = {
    "cleansing": "清潔卸妝",
    "hydration_repair": "保濕修護",
    "brightening": "美白淡斑",
    "acne_pores": "痘肌毛孔",
    "anti_aging": "抗老緊緻",
    "sensitive_soothing": "敏感舒緩",
    "sun_protection": "臉部防曬",
    "general_care": "綜合保養",
}

DEFAULT_MARKET_LABELS = {
    "taiwan": "台灣品牌",
    "japan": "日系品牌",
    "south_korea": "韓系品牌",
    "western": "歐美品牌",
    "other": "其他市場",
    "unverified": "來源待核對",
}

_FUNCTION_TERMS = {
    "hydration_repair": ("保濕", "補水", "玻尿酸", "修護", "屏障", "滋潤", "乾燥", "泛醇", "神經醯胺"),
    "brightening": ("美白", "淡斑", "亮白", "明亮", "暗沉", "傳明酸", "維他命c", "維生素c", "菸鹼醯胺"),
    "acne_pores": ("痘", "粉刺", "毛孔", "控油", "水楊酸", "杏仁酸", "壬二酸", "角質", "煥膚"),
    "anti_aging": ("抗老", "抗皺", "細紋", "緊緻", "彈力", "a醇", "視黃醇", "胜肽", "pdrn"),
    "sensitive_soothing": ("敏感", "舒緩", "低刺激", "溫和", "泛紅", "積雪草", "修復"),
}


_CAPACITY_OR_PACK_RE = re.compile(
    r"(?:(?<!\d)\d+(?:\.\d+)?\s*(?:ml|mL|ML|g|G|克|公克|毫升)\b|(?<!\d)\d+\s*(?:入|瓶|包|片|片裝|枚入|顆|支|盒|組)|補充包|補充瓶)",
    re.IGNORECASE,
)
_PROMOTION_RE = re.compile(r"(?:買\s*\d+\s*送\s*\d+|買一送一|贈品|體驗組|超值組|限定組|PICK|推薦組)", re.IGNORECASE)
_JAPANESE_KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")
_BRAND_ALIASES = {
    "nivea": "NIVEA 妮維雅",
    "nivea 妮維雅": "NIVEA 妮維雅",
    "shiseido": "SHISEIDO 資生堂",
    "資生堂": "SHISEIDO 資生堂",
    "cetaphil": "Cetaphil 舒特膚",
    "cetaphil 舒特膚": "Cetaphil 舒特膚",
    "舒特夫": "Cetaphil 舒特膚",
    "舒特膚": "Cetaphil 舒特膚",
    "paula's choice": "PAULA'S CHOICE 寶拉珍選",
    "寶拉": "PAULA'S CHOICE 寶拉珍選",
    "寶拉珍選": "PAULA'S CHOICE 寶拉珍選",
    "bio-essence": "Bio-essence 碧歐斯",
    "bio essence": "Bio-essence 碧歐斯",
    "bo essence": "Bio-essence 碧歐斯",
    "碧歐斯": "Bio-essence 碧歐斯",
    "ahc": "AHC",
    "ahc愛和純": "AHC",
    "愛和純": "AHC",
    "skin1004": "SKIN1004",
    "理膚天使": "SKIN1004",
    "dr.jart+": "Dr.Jart+",
    "dr.jart+ 蒂佳婷": "Dr.Jart+",
    "蒂佳婷": "Dr.Jart+",
    "round lab": "ROUND LAB",
    "round lab 柔恩萊": "ROUND LAB",
    "anua": "Anua",
    "vt cosmetics": "VT COSMETICS",
    "cera ve": "CeraVe 適樂膚",
    "cerave": "CeraVe 適樂膚",
    "cerave 適樂膚": "CeraVe 適樂膚",
    "la roche-posay": "La Roche-Posay 理膚寶水",
    "la roche-posay理膚寶水": "La Roche-Posay 理膚寶水",
    "肌ラボ": "肌研 Hada Labo",
    "hada-labo": "肌研 Hada Labo",
    "hada-labo肌研": "肌研 Hada Labo",
    "肌研": "肌研 Hada Labo",
    "curél": "Curél 珂潤",
    "curél珂潤": "Curél 珂潤",
    "珂潤": "Curél 珂潤",
}


def _has_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _canonical_brand_display(value: Any) -> str:
    raw = str(value or "").replace("’", "'").replace("‘", "'").strip()
    return _BRAND_ALIASES.get(raw.casefold(), raw)


def _family_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _PROMOTION_RE.sub(" ", text)
    text = _CAPACITY_OR_PACK_RE.sub(" ", text)
    text = re.sub(r"[()（）【】\[\]｜|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -_/·").casefold()


def _canonical_family_key(product: dict[str, Any]) -> str:
    brand = _canonical_brand_display(product.get("canonical_brand") or product.get("brand"))
    name = (
        product.get("taiwan_official_name")
        or product.get("display_name_zh")
        or product.get("official_name")
        or product.get("source_listing_name")
    )
    return f"{brand.casefold()}::{_family_name(name)}"


def _catalog_quality_blockers(product: dict[str, Any]) -> list[str]:
    blockers = _clean_string_list(product.get("review_blockers", []))
    display_name = str(product.get("taiwan_official_name") or "").strip()
    name_status = str(product.get("name_verification_status") or "").strip()
    verified_name_states = {"verified", "source_verified_zh", "official_tw", "verified_tw"}
    if not display_name or not _has_cjk(display_name) or _JAPANESE_KANA_RE.search(display_name):
        blockers.append("missing_verified_taiwan_name")
    if name_status not in verified_name_states:
        blockers.append("unverified_name_source")
    if _PROMOTION_RE.search(display_name) or display_name.endswith(("系列", "產品")):
        blockers.append("not_a_canonical_product")

    canonical_brand = _canonical_brand_display(product.get("canonical_brand") or product.get("brand"))
    if not canonical_brand or str(product.get("brand_origin_confidence") or "low") == "low":
        blockers.append("unverified_brand")

    ingredients = _clean_string_list(product.get("ingredients_inci", []))
    has_full_inci = str(product.get("inci_status") or "") == "full" and len(ingredients) >= 5
    featured = _clean_string_list(product.get("ingredient_tags", []))
    official_highlights = _clean_string_list(product.get("unmapped_official_highlights", []))
    has_official_featured_evidence = bool(_official_claim_evidence(product)) and bool(
        featured or official_highlights
    )
    if not has_full_inci and not has_official_featured_evidence:
        blockers.append("missing_verified_featured_ingredients")
    if has_full_inci and not str(product.get("inci_source_url") or "").strip():
        blockers.append("missing_inci_source")

    # Full INCI remains valuable, but an official product-specific featured-ingredient
    # source is now sufficient for staged catalogue import. The recommendation must
    # retain its evidence scope and must not imply allergen-complete screening.
    if has_official_featured_evidence:
        blockers = [
            blocker for blocker in blockers
            if blocker not in {"missing_full_inci", "missing_inci_source", "formula_market_not_verified_tw"}
        ]
    if product.get("duplicate_of"):
        blockers.append("duplicate_candidate")
    return list(dict.fromkeys(blockers))


class ProductImportError(RuntimeError):
    """Approval was recorded, but the product could not enter the live catalog."""

    def __init__(self, message: str, *, product: dict[str, Any]) -> None:
        super().__init__(message)
        self.product = product


def _stable_review_sku(canonical_key: str) -> str:
    digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:16].upper()
    return f"BLINK-REVIEW-{digest}"


def _first_source_url(product: dict[str, Any]) -> str:
    for evidence in product.get("evidence", []):
        value = str(evidence.get("source_url") or "").strip()
        if value:
            return value
    return ""


def _popularity_metadata(product: dict[str, Any]) -> dict[str, Any]:
    """Create a small, auditable acceptance signal; never treat it as efficacy."""
    cohort = str(product.get("cohort") or "").strip().lower()
    cohort_weight = {
        "classic": 0.70,
        "annual": 0.62,
        "trending": 0.58,
        "recent": 0.50,
        "new_release": 0.35,
    }.get(cohort, 0.0)
    evidence = [item for item in product.get("evidence", []) if isinstance(item, dict)]
    ranks = []
    labels = []
    for item in evidence:
        try:
            rank = int(item.get("rank"))
            if rank > 0:
                ranks.append(rank)
        except (TypeError, ValueError):
            pass
        context = str(item.get("context") or "").strip()
        source_name = str(item.get("source_name") or item.get("source_id") or "").strip()
        label = " · ".join(part for part in (source_name, context) if part)
        if label:
            labels.append(label[:180])
    rank_bonus = max((0.30 / max(rank, 1) for rank in ranks), default=0.0)
    evidence_bonus = min(len(evidence), 3) * 0.08
    return {
        "cohort": cohort,
        "trend_evidence": list(dict.fromkeys(labels))[:3],
        "evidence_count": len(evidence),
        "best_evidence_rank": min(ranks) if ranks else 0,
        "popularity_score": round(min(1.0, cohort_weight + rank_bonus + evidence_bonus), 4),
    }


def _product_search_text(product: dict[str, Any]) -> str:
    values = [
        product.get("taiwan_official_name"),
        product.get("official_name"),
        product.get("source_listing_name"),
        product.get("original_name"),
        product.get("canonical_brand"),
        product.get("category"),
        " ".join(product.get("common_names", [])),
        " ".join(product.get("search_aliases", [])),
        " ".join(product.get("concerns", [])),
        " ".join(product.get("ingredients_inci", [])),
        " ".join(product.get("ingredient_tags", [])),
        " ".join(product.get("unmapped_official_highlights", [])),
    ]
    return " ".join(str(value).strip() for value in values if str(value or "").strip())


def _product_sync_fingerprint(product: dict[str, Any]) -> str:
    fields = {
        key: product.get(key)
        for key in (
            "taiwan_official_name",
            "display_name_zh",
            "official_name",
            "canonical_brand",
            "category",
            "common_names",
            "search_aliases",
            "concerns",
            "ingredients_inci",
            "ingredient_tags",
            "unmapped_official_highlights",
            "claim_evidence",
            "safety_notes",
            "formula_market",
            "inci_source_url",
        )
    }
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _import_product_to_neo4j(
    product: dict[str, Any],
    *,
    reviewer_id_hash: str,
    imported_at: str,
) -> dict[str, Any]:
    blockers = _catalog_quality_blockers(product)
    if blockers:
        raise ValueError("商品尚未通過品質檢查：" + ", ".join(blockers))

    name = str(product.get("taiwan_official_name") or "").strip()
    vector = EmbeddingClient.from_env().embed_query(_product_search_text(product))
    expected_dimension = max(1, int(os.getenv("EMBEDDING_DIMENSION", "1024")))
    if len(vector) != expected_dimension:
        raise ValueError(f"商品向量維度錯誤：預期 {expected_dimension}，實際 {len(vector)}")

    uri = os.getenv("NEO4J_URI", "").strip()
    user = os.getenv("NEO4J_USER", "").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()
    if not all((uri, user, password)):
        raise RuntimeError("Neo4j 連線設定不完整")

    source_urls = [
        str(evidence.get("source_url") or "").strip()
        for evidence in [
            *product.get("evidence", []),
            *product.get("claim_evidence", []),
        ]
        if isinstance(evidence, dict)
        and str(evidence.get("source_url") or "").strip()
    ]
    popularity = _popularity_metadata(product)
    ingredients_inci = _clean_string_list(product.get("ingredients_inci", []))
    ingredient_tags = _clean_string_list(product.get("ingredient_tags", []))
    unmapped_highlights = _clean_string_list(product.get("unmapped_official_highlights", []))
    safety_notes = _ingredient_safety_notes(product)
    ingredient_evidence_level = str(product.get("ingredient_evidence_level") or "unverified")
    recommendation_scope = (
        "full_formula" if ingredient_evidence_level == "full_inci"
        else "official_featured_ingredients_only"
    )
    properties: dict[str, Any] = {
        "name": name,
        "official_name": str(product.get("official_name") or "").strip(),
        "taiwan_official_name": name,
        "display_name_zh": str(product.get("display_name_zh") or name).strip(),
        "original_name": str(product.get("original_name") or "").strip(),
        "source_listing_name": str(product.get("source_listing_name") or "").strip(),
        "canonical_brand": _canonical_brand_display(product.get("canonical_brand") or product.get("brand")),
        "brand": _canonical_brand_display(product.get("canonical_brand") or product.get("brand")),
        "canonical_family_key": _canonical_family_key(product),
        "category": str(product.get("category") or "臉部保養").strip(),
        "ingredients_list": ingredients_inci,
        "ingredients_inci": ingredients_inci,
        "ingredient_tags": ingredient_tags,
        "unmapped_official_highlights": unmapped_highlights,
        "ingredient_evidence_level": ingredient_evidence_level,
        "inci_status": str(product.get("inci_status") or "missing"),
        "recommendation_scope": recommendation_scope,
        "recommendation_limitations": (
            "已核對台灣版完整 INCI。"
            if ingredient_evidence_level == "full_inci"
            else "僅依官方主打成分與聲稱推薦；未完成完整 INCI，不可宣稱已排除所有過敏原或禁忌。"
        ),
        "safety_warnings": _clean_string_list([note.get("message") for note in safety_notes]),
        "safety_source_urls": _clean_string_list([note.get("source_url") for note in safety_notes]),
        "inci_source_url": str(product.get("inci_source_url") or "").strip(),
        "formula_market": str(product.get("formula_market") or "").strip(),
        "common_names": _clean_string_list(product.get("common_names", [])),
        "search_aliases": _clean_string_list(product.get("search_aliases", [])),
        "concerns": _clean_string_list(product.get("concerns", [])),
        "functional_categories": _clean_string_list(product.get("functional_categories", [])),
        "source_urls": list(dict.fromkeys(source_urls)),
        "source": "product_review",
        "source_type": "admin_approved",
        "source_url": _first_source_url(product),
        "review_canonical_key": str(product["canonical_key"]),
        "reviewer_id_hash": reviewer_id_hash,
        "name_verification_status": str(product.get("name_verification_status") or "source_listing"),
        "brand_market_key": str(product.get("brand_market_key") or "unverified"),
        "brand_origin_country": str(product.get("brand_origin_country") or "待核對"),
        "cohort": popularity["cohort"],
        "trend_evidence": popularity["trend_evidence"],
        "evidence_count": popularity["evidence_count"],
        "best_evidence_rank": popularity["best_evidence_rank"],
        "popularity_score": popularity["popularity_score"],
        "priority": 3,
        "_new": 1,
        "is_partner": False,
        "is_active": True,
        "recommendable": True,
    }
    for price_field in ("price", "list_price", "sale_price"):
        raw_price = product.get(price_field)
        if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool) and raw_price > 0:
            properties[price_field] = float(raw_price)
    for text_field in ("currency", "capacity", "price_status", "price_source_url", "price_observed_at"):
        value = str(product.get(text_field) or "").strip()
        if value:
            properties[text_field] = value

    sku = _stable_review_sku(_canonical_family_key(product))
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            record = session.run(
                """
                MERGE (p:Product {sku: $sku})
                ON CREATE SET p.created_at = $now
                SET p += $properties,
                    p.embedding = $embedding,
                    p.updated_at = $now
                RETURN elementId(p) AS neo4j_element_id
                """,
                sku=sku,
                now=imported_at,
                properties=properties,
                embedding=vector,
            ).single()
        if record is None:
            raise RuntimeError("Neo4j 未回傳商品節點")
        return {
            "import_status": "imported",
            "imported_at": imported_at,
            "import_error": "",
            "neo4j_sku": sku,
            "neo4j_element_id": record["neo4j_element_id"],
            "recommendable": True,
        }
    finally:
        driver.close()


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _source_definitions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for seed_path in _SEED_FILES:
        seed = _read_json(seed_path, {})
        raw_sources = seed.get("sources", {}) if isinstance(seed, dict) else {}
        if isinstance(raw_sources, dict):
            for source_id, source in raw_sources.items():
                if isinstance(source, dict):
                    result[str(source_id)] = {"source_id": str(source_id), **source}
    manifest = _read_json(_SOURCE_MANIFEST, {})
    for source in manifest.get("sources", []) if isinstance(manifest, dict) else []:
        if isinstance(source, dict) and source.get("source_id"):
            result[str(source["source_id"])] = source
    return result


def _normalized_brand(value: Any) -> str:
    return (
        str(value or "")
        .replace("’", "'")
        .replace("‘", "'")
        .strip()
        .casefold()
    )


def _brand_market_registry() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    payload = _read_json(_BRAND_MARKET_REGISTRY, {})
    labels = {
        **DEFAULT_MARKET_LABELS,
        **(payload.get("markets", {}) if isinstance(payload, dict) else {}),
    }
    raw_brands = payload.get("brands", {}) if isinstance(payload, dict) else {}
    brands = {
        _normalized_brand(brand): {"registry_brand": brand, **metadata}
        for brand, metadata in raw_brands.items()
        if isinstance(metadata, dict)
    }
    return labels, brands


def _product_name_registry() -> dict[str, dict[str, Any]]:
    payload = _read_json(_PRODUCT_NAME_REGISTRY, {})
    raw_products = payload.get("products", {}) if isinstance(payload, dict) else {}
    return {
        str(canonical_key): metadata
        for canonical_key, metadata in raw_products.items()
        if isinstance(metadata, dict)
    }


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        item = str(raw_item or "").strip()
        normalized = item.casefold()
        if item and normalized not in seen:
            seen.add(normalized)
            result.append(item)
    return result


_INGREDIENT_TAG_ALIASES = {
    "玻尿酸": ("玻尿酸", "透明質酸", "hyaluronic acid", "sodium hyaluronate", "hyalu"),
    "神經醯胺": ("神經醯胺", "ceramide"),
    "菸鹼醯胺": ("菸鹼醯胺", "煙醯胺", "niacinamide"),
    "傳明酸": ("傳明酸", "tranexamic acid", "txa"),
    "維他命 C": ("維他命 c", "維生素 c", "vitamin c", "ascorbic acid"),
    "水楊酸": ("水楊酸", "salicylic acid", "bha"),
    "杏仁酸": ("杏仁酸", "mandelic acid"),
    "壬二酸": ("壬二酸", "azelaic acid"),
    "果酸": ("果酸", "glycolic acid", "lactic acid", "aha"),
    "視黃醇": ("a醇", "視黃醇", "retinol", "retinal"),
    "補骨脂酚": ("補骨脂酚", "bakuchiol"),
    "胜肽": ("胜肽", "peptide"),
    "積雪草": ("積雪草", "centella", "cica"),
    "泛醇 B5": ("泛醇", "panthenol", "cicaplast baume b5", "b5 修復"),
    "角鯊烷": ("角鯊烷", "squalane"),
    "尿囊素": ("尿囊素", "allantoin"),
    "熊果素": ("熊果素", "arbutin"),
    "PDRN": ("pdrn",),
    "羥基積雪草苷": ("羥基積雪草苷", "madecassoside"),
}


def _official_claim_evidence(product: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in product.get("claim_evidence", [])
        if isinstance(item, dict)
        and str(item.get("evidence_type") or "")
        in {"official_claim", "ingredient_supported", "official_full_inci"}
        and str(item.get("source_url") or "").strip()
    ]


def _ingredient_tags(product: dict[str, Any]) -> list[str]:
    """Return only explicit or officially evidenced ingredient mentions.

    Product names, concerns and efficacy labels are deliberately excluded: using
    them to infer ingredients caused false tags in earlier review batches.
    """
    tags = _clean_string_list(product.get("ingredient_tags") or [])
    claim_text = " ".join(
        str(item.get("claim") or item.get("context") or "")
        for item in _official_claim_evidence(product)
    ).casefold()
    for canonical, aliases in _INGREDIENT_TAG_ALIASES.items():
        if claim_text and any(alias.casefold() in claim_text for alias in aliases):
            tags.append(canonical)
    return _clean_string_list(tags)


_INGREDIENT_SAFETY_RULES = (
    {
        "rule_id": "retinoid_pregnancy",
        "aliases": ("retinol", "retinal", "retinoid", "retinoate", "視黃醇", "a醇", "a 醇", "adapalene", "tretinoin", "tazarotene"),
        "audiences": ["孕婦", "備孕者", "哺乳期使用者"],
        "severity": "avoid_or_consult",
        "message": "孕婦與備孕期間應避免使用外用維生素 A／視黃醇類；哺乳期使用前請先詢問醫師。",
        "source_name": "American Academy of Dermatology",
        "source_url": "https://www.aad.org/public/everyday-care/skin-care-secrets/routine/pregnancy-skin-care",
    },
    {
        "rule_id": "retinoid_sensitive_skin",
        "aliases": ("retinol", "retinal", "retinoid", "retinoate", "視黃醇", "a醇", "a 醇"),
        "audiences": ["敏感肌", "乾燥肌", "酒糟或正發炎肌膚"],
        "severity": "caution",
        "message": "視黃醇類可能造成刺激；明顯乾燥、泛紅、酒糟或發炎時應降低頻率或先諮詢皮膚科醫師。",
        "source_name": "American Academy of Dermatology",
        "source_url": "https://www.aad.org/public/everyday-care/skin-care-secrets/anti-aging/retinoid-retinol",
    },
    {
        "rule_id": "aha_sun_sensitivity",
        "aliases": ("glycolic acid", "lactic acid", "mandelic acid", "alpha hydroxy acid", "aha", "果酸", "甘醇酸", "乳酸", "杏仁酸"),
        "audiences": ["所有使用者", "容易曬傷者"],
        "severity": "sun_protection",
        "message": "AHA 果酸可能提高日曬敏感度；使用期間及停用後一週應確實防曬並減少曝曬。",
        "source_name": "U.S. Food and Drug Administration",
        "source_url": "https://www.fda.gov/regulatory-information/search-fda-guidance-documents/guidance-industry-labeling-cosmetics-containing-alpha-hydroxy-acids",
    },
    {
        "rule_id": "bha_child_irritation",
        "aliases": ("salicylic acid", "beta hydroxy acid", "bha", "水楊酸"),
        "audiences": ["嬰幼兒與兒童", "敏感肌"],
        "severity": "caution",
        "message": "BHA／水楊酸應先局部測試；若持續刺痛或刺激應停用，且不建議用於嬰幼兒與兒童。",
        "source_name": "U.S. Food and Drug Administration",
        "source_url": "https://www.fda.gov/cosmetics/cosmetic-ingredients/beta-hydroxy-acids",
    },
    {
        "rule_id": "salicylic_acid_pregnancy_concentration",
        "aliases": ("salicylic acid", "beta hydroxy acid", "bha", "水楊酸"),
        "audiences": ["孕婦", "備孕者"],
        "severity": "check_concentration",
        "message": "孕期高濃度（超過 2%）水楊酸應限制使用並先諮詢醫師；濃度未公開時不可自行判定安全。",
        "source_name": "American Academy of Dermatology",
        "source_url": "https://www.aad.org/public/everyday-care/skin-care-secrets/routine/pregnancy-skin-care",
    },
)


def _ingredient_safety_notes(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Build conservative warnings from verified product and authority evidence."""
    explicit = product.get("contraindication_notes", [])
    if not isinstance(explicit, list):
        explicit = []
    claim_source_url = next(
        (
            str(item.get("source_url") or "").strip()
            for item in product.get("claim_evidence", [])
            if isinstance(item, dict) and str(item.get("source_url") or "").strip()
        ),
        "",
    )
    fallback_source_url = str(product.get("source_url") or claim_source_url or "").strip()
    notes: list[dict[str, Any]] = []

    def audiences_from_message(message: str) -> list[str]:
        rules = (
            (("孕婦", "孕期", "懷孕", "備孕", "哺乳"), "孕婦／備孕／哺乳族群"),
            (("嬰兒", "嬰幼兒", "兒童"), "嬰幼兒與兒童"),
            (("敏感", "過敏", "酒糟"), "敏感肌／過敏族群"),
            (("傷口", "破皮", "術後", "療程後"), "傷口／醫美術後肌膚"),
            (("眼周", "眼睛", "黏膜"), "眼周／黏膜接觸"),
            (("處方",), "正在使用處方產品者"),
        )
        return [label for terms, label in rules if any(term in message for term in terms)]

    for item in explicit:
        if isinstance(item, dict):
            message = str(item.get("message") or item.get("note") or "").strip()
            source_url = str(
                item.get("source_url") or item.get("evidence_url") or fallback_source_url
            ).strip()
            audiences = _clean_string_list(item.get("audiences", []))
            population = str(item.get("population") or "").strip()
            population_labels = {
                "pregnant_or_breastfeeding": "懷孕／哺乳族群",
                "sensitive_skin": "敏感肌",
                "children": "兒童",
            }
            if population:
                audiences.append(population_labels.get(population, population))
            audiences.extend(audiences_from_message(message))
            if message and source_url:
                notes.append(
                    {
                        **item,
                        "message": message,
                        "source_url": source_url,
                        "source_name": str(
                            item.get("source_name") or "品牌官方使用注意"
                        ),
                        "audiences": list(dict.fromkeys(audiences)),
                        "severity": str(item.get("severity") or "product_label_caution"),
                    }
                )
            continue
        if isinstance(item, str):
            raw_message = item.strip()
            url_match = re.search(r"https?://[^\s，。]+", raw_message)
            source_url = (url_match.group(0).rstrip(".,;)") if url_match else fallback_source_url)
            message = re.sub(r"(?:證據|依據)[:：]?\s*https?://[^\s，。]+", "", raw_message).strip(" ；;")
            if message and source_url:
                notes.append(
                    {
                        "message": message,
                        "source_name": "品牌官方使用注意",
                        "source_url": source_url,
                        "audiences": audiences_from_message(message),
                        "severity": "product_label_caution",
                    }
                )

    searchable = " ".join(
        [
            *_clean_string_list(product.get("ingredient_tags", [])),
            *_clean_string_list(product.get("unmapped_official_highlights", [])),
        ]
    ).casefold()
    for rule in _INGREDIENT_SAFETY_RULES:
        if searchable and any(alias.casefold() in searchable for alias in rule["aliases"]):
            notes.append({key: value for key, value in rule.items() if key != "aliases"})
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for note in notes:
        identity = str(note.get("rule_id") or note.get("message") or "").casefold()
        if identity and identity not in seen:
            seen.add(identity)
            unique.append(note)
    return unique

def _suggested_functional_categories(product: dict[str, Any]) -> list[str]:
    category = str(product.get("category") or "").casefold()
    concerns = " ".join(str(value) for value in product.get("concerns", []))
    name = f"{product.get('name_zh', '')} {product.get('name_en', '')}"
    searchable = f"{category} {concerns} {name}".casefold()
    result: list[str] = []
    if any(term in searchable for term in ("洗面", "洗顏", "潔顏", "卸妝", "清潔")):
        result.append("cleansing")
    if "防曬" in searchable or "spf" in searchable:
        result.append("sun_protection")
    for key, terms in _FUNCTION_TERMS.items():
        if any(term in searchable for term in terms):
            result.append(key)
    return list(dict.fromkeys(result))


def _functional_categories(product: dict[str, Any]) -> list[str]:
    explicit = _clean_string_list(product.get("verified_functional_categories", []))
    verified = [key for key in explicit if key in FUNCTION_LABELS]
    claim_text = " ".join(
        str(item.get("claim") or item.get("context") or "")
        for item in product.get("claim_evidence", [])
        if isinstance(item, dict)
        and str(item.get("evidence_type") or "") in {"official_claim", "ingredient_supported"}
        and str(item.get("source_url") or "").strip()
    ).casefold()
    if claim_text:
        if any(term in claim_text for term in ("洗面", "洗顏", "潔顏", "卸妝", "清潔")):
            verified.append("cleansing")
        if "防曬" in claim_text or "spf" in claim_text:
            verified.append("sun_protection")
        for key, terms in _FUNCTION_TERMS.items():
            if any(term in claim_text for term in terms):
                verified.append(key)
    return list(dict.fromkeys(verified)) or ["general_care"]


def _load_decisions() -> dict[str, dict[str, Any]]:
    payload = _read_json(_DECISIONS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def _verified_enrichments() -> dict[str, dict[str, Any]]:
    payload = _read_json(_VERIFIED_ENRICHMENTS_FILE, {})
    records = payload.get("products", {}) if isinstance(payload, dict) else {}
    return {str(key): value for key, value in records.items() if isinstance(value, dict)}


def _load_products() -> list[dict[str, Any]]:
    sources = _source_definitions()
    enrichments = _verified_enrichments()
    market_labels, brand_registry = _brand_market_registry()
    name_registry = _product_name_registry()
    decisions = _load_decisions()
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seed_path in _SEED_FILES:
        payload = _read_json(seed_path, {})
        for raw in payload.get("products", []) if isinstance(payload, dict) else []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("canonical_key") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            enrichment = enrichments.get(key, {})
            if enrichment:
                raw = {
                    **raw,
                    **enrichment,
                    "evidence": [
                        *(raw.get("evidence", []) if isinstance(raw.get("evidence"), list) else []),
                        *(enrichment.get("evidence", []) if isinstance(enrichment.get("evidence"), list) else []),
                    ],
                }
            decision = decisions.get(key, {})
            evidence = []
            for raw_evidence in raw.get("evidence", []):
                if not isinstance(raw_evidence, dict):
                    continue
                source_id = str(raw_evidence.get("source_id") or "")
                source = sources.get(source_id, {})
                evidence.append(
                    {
                        **raw_evidence,
                        "source_id": source_id,
                        "source_name": source.get("name") or raw_evidence.get("source_name") or source_id,
                        "source_url": source.get("url") or raw_evidence.get("source_url"),
                        "time_bucket": source.get("time_bucket") or raw.get("cohort"),
                        "period_start": source.get("period_start") or source.get("period"),
                        "period_end": source.get("period_end"),
                    }
                )
            function_keys = _functional_categories(raw)
            suggested_function_keys = _suggested_functional_categories(raw)
            brand = str(raw.get("brand") or "").strip()
            brand_metadata = brand_registry.get(_normalized_brand(brand), {})
            canonical_brand = _canonical_brand_display(brand_metadata.get("canonical_brand") or brand)
            brand_market_key = str(raw.get("brand_market_key") or brand_metadata.get("market") or "unverified")
            name_metadata = name_registry.get(key, {})
            source_name_zh = str(raw.get("name_zh") or "").strip()
            source_name_en = str(raw.get("name_en") or "").strip()
            source_listing_name = str(
                raw.get("source_listing_name") or source_name_zh or source_name_en
            ).strip()
            raw_name_status = str(
                raw.get("name_verification_status")
                or raw.get("name_zh_status")
                or ""
            ).strip()
            requires_official_zh = (
                raw_name_status == "needs_official_chinese_name"
                or "missing_official_chinese_name"
                in _clean_string_list(raw.get("review_blockers", []))
            )
            official_name = str(
                name_metadata.get("official_name")
                or raw.get("official_name")
                or source_name_en
                or source_name_zh
            ).strip()
            verified_name_states = {"verified", "source_verified_zh", "official_tw", "verified_tw"}
            candidate_tw_name = str(
                name_metadata.get("taiwan_official_name")
                or raw.get("taiwan_official_name")
                or ""
            ).strip()
            if (
                not candidate_tw_name
                and raw_name_status in verified_name_states
                and _has_cjk(source_name_zh)
                and not _JAPANESE_KANA_RE.search(source_name_zh)
            ):
                candidate_tw_name = source_name_zh
            taiwan_official_name = candidate_tw_name
            if taiwan_official_name and (
                not _has_cjk(taiwan_official_name)
                or _JAPANESE_KANA_RE.search(taiwan_official_name)
            ):
                taiwan_official_name = ""
                requires_official_zh = True
            display_name_zh = str(
                name_metadata.get("display_name_zh")
                or raw.get("display_name_zh")
                or taiwan_official_name
                or (source_name_zh if _has_cjk(source_name_zh) and not _JAPANESE_KANA_RE.search(source_name_zh) else "")
            ).strip()
            original_name = str(
                name_metadata.get("original_name")
                or raw.get("original_name")
                or source_name_en
                or source_listing_name
            ).strip()
            common_names = _clean_string_list(
                [
                    *name_metadata.get("common_names", []),
                    *raw.get("common_names", []),
                ]
            )
            search_aliases = _clean_string_list(
                [
                    *name_metadata.get("search_aliases", []),
                    *raw.get("search_aliases", []),
                ]
            )
            inci_candidate = (
                name_metadata.get("ingredients_inci")
                or raw.get("ingredients_inci")
                or (
                    raw.get("ingredients_list")
                    if str(raw.get("ingredient_evidence_level") or "") == "full_inci"
                    else []
                )
            )
            ingredients_inci = _clean_string_list(inci_candidate)
            ingredients_source_text = str(
                name_metadata.get("ingredients_source_text")
                or raw.get("ingredients_source_text")
                or raw.get("ingredients_source_text_zh")
                or ""
            ).strip()
            ingredients_source_language = str(
                name_metadata.get("ingredients_source_language")
                or raw.get("ingredients_source_language")
                or ""
            ).strip()
            ingredient_normalization_status = str(
                name_metadata.get("ingredient_normalization_status")
                or raw.get("ingredient_normalization_status")
                or ("verified_latin_inci" if ingredients_inci else "missing")
            ).strip()
            inci_source_url = str(
                name_metadata.get("inci_source_url")
                or raw.get("inci_source_url")
                or ""
            ).strip()
            formula_market = str(
                name_metadata.get("formula_market")
                or raw.get("formula_market")
                or ""
            ).strip()
            inci_status = "full" if ingredients_inci and inci_source_url and formula_market else "missing"
            ingredient_tags = _ingredient_tags(
                {
                    **raw,
                    "official_name": official_name,
                    "taiwan_official_name": taiwan_official_name,
                    "evidence": evidence,
                }
            )
            unmapped_official_highlights = _clean_string_list(
                raw.get("unmapped_official_highlights", [])
            )
            claim_evidence = [
                dict(item) for item in raw.get("claim_evidence", [])
                if isinstance(item, dict)
            ]
            official_featured_evidence = bool(
                _official_claim_evidence({"claim_evidence": claim_evidence})
                and (ingredient_tags or unmapped_official_highlights)
            )
            ingredient_evidence_level = (
                "full_inci" if inci_status == "full"
                else "verified_tags" if official_featured_evidence
                else "unverified"
            )
            safety_notes = _ingredient_safety_notes(
                {
                    **raw,
                    "ingredient_tags": ingredient_tags,
                    "unmapped_official_highlights": unmapped_official_highlights,
                }
            )
            name_verification_status = str(
                name_metadata.get("verification_status")
                or raw_name_status
                or "source_listing"
            )
            review_blockers = _clean_string_list(raw.get("review_blockers", []))
            if name_verification_status in verified_name_states and taiwan_official_name:
                requires_official_zh = False
                review_blockers = [
                    blocker for blocker in review_blockers
                    if blocker != "missing_official_chinese_name"
                ]
            if requires_official_zh and "missing_official_chinese_name" not in review_blockers:
                review_blockers.append("missing_official_chinese_name")
            if official_featured_evidence:
                review_blockers = [
                    blocker for blocker in review_blockers
                    if blocker not in {"missing_full_inci", "missing_inci_source", "formula_market_not_verified_tw"}
                ]
            loaded_product = {
                    **raw,
                    "source_listing_name": source_listing_name,
                    "official_name": official_name,
                    "taiwan_official_name": taiwan_official_name,
                    "display_name_zh": display_name_zh,
                    "original_name": original_name,
                    "common_names": common_names,
                    "search_aliases": search_aliases,
                    "name_verification_status": name_verification_status,
                    "name_zh_status": name_verification_status,
                    "review_blockers": review_blockers,
                    "name_confidence": str(raw.get("name_confidence") or name_metadata.get("confidence") or "low"),
                    "name_sources": _clean_string_list([*name_metadata.get("sources", []), *raw.get("name_sources", [])]),
                    "ingredients_inci": ingredients_inci,
                    "ingredients_source_text": ingredients_source_text,
                    "ingredients_source_language": ingredients_source_language,
                    "ingredient_normalization_status": ingredient_normalization_status,
                    "inci_source_url": inci_source_url,
                    "formula_market": formula_market,
                    "ingredient_tags": ingredient_tags,
                    "unmapped_official_highlights": unmapped_official_highlights,
                    "claim_evidence": claim_evidence,
                    "ingredient_evidence_level": ingredient_evidence_level,
                    "ingredient_highlights_status": (
                        "verified" if official_featured_evidence or inci_status == "full"
                        else "missing"
                    ),
                    "safety_notes": safety_notes,
                    "recommendation_scope": (
                        "full_formula" if ingredient_evidence_level == "full_inci"
                        else "official_featured_ingredients_only"
                        if ingredient_evidence_level == "verified_tags"
                        else "not_verified"
                    ),
                    "recommendation_limitations": (
                        "已核對台灣版完整 INCI。"
                        if ingredient_evidence_level == "full_inci"
                        else "僅依官方主打成分與聲稱；未完成完整 INCI，無法排除所有過敏原或個人禁忌。"
                        if ingredient_evidence_level == "verified_tags"
                        else "尚未取得可核對的官方主打成分。"
                    ),
                    "inci_status": inci_status,
                    "brand": brand,
                    "canonical_brand": canonical_brand,
                    "brand_market_key": brand_market_key,
                    "brand_market_label": market_labels.get(
                        brand_market_key,
                        market_labels["unverified"],
                    ),
                    "brand_origin_country": raw.get("brand_origin_country") or brand_metadata.get("country_zh") or "待核對",
                    "brand_origin_confidence": raw.get("brand_origin_confidence") or brand_metadata.get("confidence") or "low",
                    "manufacturer": str(raw.get("manufacturer") or brand_metadata.get("manufacturer") or "").strip(),
                    "brand_owner": str(raw.get("brand_owner") or brand_metadata.get("brand_owner") or "").strip(),
                    "tw_distributor": str(raw.get("tw_distributor") or brand_metadata.get("tw_distributor") or "").strip(),
                    "brand_source_url": str(raw.get("brand_source_url") or brand_metadata.get("source_url") or "").strip(),
                    "functional_categories": function_keys,
                    "functional_labels": [FUNCTION_LABELS[key] for key in function_keys],
                    "suggested_functional_categories": suggested_function_keys,
                    "suggested_functional_labels": [FUNCTION_LABELS[key] for key in suggested_function_keys],
                    "evidence": evidence,
                    "review_status": decision.get("review_status", "pending_review"),
                    "reviewer_notes": decision.get("reviewer_notes", ""),
                    "duplicate_of": decision.get("duplicate_of") or raw.get("duplicate_of"),
                    "reviewed_at": decision.get("reviewed_at"),
                    "import_status": decision.get("import_status", "not_requested"),
                    "imported_at": decision.get("imported_at"),
                    "import_error": decision.get("import_error", ""),
                    "neo4j_sku": decision.get("neo4j_sku"),
                    "neo4j_element_id": decision.get("neo4j_element_id"),
                    "sync_fingerprint": decision.get("sync_fingerprint"),
                    "recommendable": bool(decision.get("recommendable", False)),
                }
            loaded_product["canonical_family_key"] = _canonical_family_key(loaded_product)
            loaded_product["quality_blockers"] = _catalog_quality_blockers(loaded_product)
            loaded_product["review_blockers"] = _clean_string_list([*loaded_product.get("review_blockers", []), *loaded_product["quality_blockers"]])
            products.append(loaded_product)
    family_groups: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        family_key = str(product.get("canonical_family_key") or "")
        if family_key and not family_key.endswith("::"):
            family_groups.setdefault(family_key, []).append(product)
    for group in family_groups.values():
        if len(group) < 2:
            continue
        capacities = _clean_string_list([item.get("capacity") for item in group])
        active_group = [
            item
            for item in group
            if item.get("review_status") not in {"duplicate", "excluded"}
        ]
        active_keys = [str(item["canonical_key"]) for item in active_group]
        for product in group:
            product["capacity_variants"] = capacities
            product["duplicate_candidates"] = [
                key for key in active_keys if key != product["canonical_key"]
            ]
            if len(active_group) < 2 or product not in active_group:
                continue
            product["review_blockers"] = _clean_string_list([
                *product.get("review_blockers", []),
                "possible_size_variant_duplicate",
            ])
            product["quality_blockers"] = _clean_string_list([
                *product.get("quality_blockers", []),
                "possible_size_variant_duplicate",
            ])
    return products


def list_product_reviews(
    *,
    search: str = "",
    review_status: str = "",
    functional_category: str = "",
    cohort: str = "",
    brand_market: str = "",
    brand: str = "",
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    products = _load_products()
    needle = search.strip().casefold()
    if needle:
        products = [
            product
            for product in products
            if needle
            in " ".join(
                [
                    str(product.get("brand") or ""),
                    str(product.get("name_zh") or ""),
                    str(product.get("name_en") or ""),
                    str(product.get("official_name") or ""),
                    str(product.get("taiwan_official_name") or ""),
                    str(product.get("original_name") or ""),
                    " ".join(product.get("common_names", [])),
                    " ".join(product.get("search_aliases", [])),
                    " ".join(product.get("ingredients_inci", [])),
                    str(product.get("category") or ""),
                    " ".join(str(value) for value in product.get("concerns", [])),
                ]
            ).casefold()
        ]
    if review_status:
        products = [product for product in products if product["review_status"] == review_status]
    if functional_category:
        products = [
            product
            for product in products
            if functional_category in product["functional_categories"]
        ]
    if cohort:
        products = [product for product in products if product.get("cohort") == cohort]
    if brand_market:
        products = [
            product for product in products if product["brand_market_key"] == brand_market
        ]
    if brand:
        normalized_brand = _normalized_brand(brand)
        products = [
            product
            for product in products
            if _normalized_brand(product.get("canonical_brand")) == normalized_brand
        ]
    products.sort(
        key=lambda product: (
            product["review_status"] != "pending_review",
            str(product.get("functional_labels", [""])[0]),
            str(product.get("brand") or ""),
            str(product.get("name_zh") or ""),
        )
    )
    total = len(products)
    start = (page - 1) * limit
    return {
        "items": products[start : start + limit],
        "total": total,
        "page": page,
        "limit": limit,
    }


def product_review_stats() -> dict[str, Any]:
    products = _load_products()
    market_labels, _ = _brand_market_registry()
    status_counts = Counter(product["review_status"] for product in products)
    import_status_counts = Counter(product["import_status"] for product in products)
    function_counts: Counter[str] = Counter()
    cohort_counts = Counter(str(product.get("cohort") or "unknown") for product in products)
    category_counts = Counter(str(product.get("category") or "未分類") for product in products)
    market_counts = Counter(product["brand_market_key"] for product in products)
    brand_counts = Counter(product["canonical_brand"] for product in products)
    naming_verified = sum(
        product["name_verification_status"] in {"verified", "source_verified_zh", "official_tw", "verified_tw"}
        for product in products
    )
    with_common_names = sum(bool(product["common_names"]) for product in products)
    with_inci = sum(product["inci_status"] == "full" for product in products)
    with_featured_ingredients = sum(
        product.get("ingredient_highlights_status") == "verified" for product in products
    )
    with_safety_notes = sum(bool(product.get("safety_notes")) for product in products)

    identity_hold_blockers = {
        "official_product_identity_hold",
        "version_identity_hold",
        "version_mismatch_hold",
        "suspected_hallucinated_name",
        "not_a_canonical_product",
        "duplicate_candidate",
    }
    active_candidates = [
        product for product in products
        if product.get("review_status") in {"approved", "needs_enrichment"}
    ]
    ingredient_identity_holds = [
        product for product in active_candidates
        if identity_hold_blockers.intersection(product.get("review_blockers", []))
    ]
    researchable_products = [
        product for product in active_candidates
        if not identity_hold_blockers.intersection(product.get("review_blockers", []))
    ]
    featured_ingredient_eligible_total = len(researchable_products)
    with_featured_ingredients_eligible = sum(
        product.get("ingredient_highlights_status") == "verified"
        for product in researchable_products
    )
    missing_featured_ingredients_researchable = (
        featured_ingredient_eligible_total - with_featured_ingredients_eligible
    )
    not_in_active_catalog = sum(
        product.get("review_status") in {"pending_review", "duplicate", "excluded"}
        for product in products
    )

    for product in products:
        function_counts.update(product["functional_categories"])
    return {
        "total": len(products),
        "status_counts": dict(status_counts),
        "import_status_counts": dict(import_status_counts),
        "functional_categories": [
            {"key": key, "label": label, "count": function_counts.get(key, 0)}
            for key, label in FUNCTION_LABELS.items()
        ],
        "cohort_counts": dict(cohort_counts),
        "catalog_categories": dict(category_counts),
        "brand_markets": [
            {"key": key, "label": label, "count": market_counts.get(key, 0)}
            for key, label in market_labels.items()
        ],
        "brands": [
            {"brand": brand, "count": count}
            for brand, count in sorted(
                brand_counts.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        ],
        "name_coverage": {
            "verified_names": naming_verified,
            "source_listing_names": len(products) - naming_verified,
            "with_common_names": with_common_names,
            "with_inci": with_inci,
            "missing_inci": len(products) - with_inci,
            "with_featured_ingredients": with_featured_ingredients,
            "missing_featured_ingredients_all_records": len(products) - with_featured_ingredients,
            "featured_ingredient_eligible_total": featured_ingredient_eligible_total,
            "with_featured_ingredients_eligible": with_featured_ingredients_eligible,
            "missing_featured_ingredients_researchable": missing_featured_ingredients_researchable,
            "ingredient_identity_holds": len(ingredient_identity_holds),
            "not_in_active_catalog": not_in_active_catalog,
            "with_safety_notes": with_safety_notes,
        },
        "notice": "台灣正式品名與品牌需先核對；成分至少要有精確商品頁的官方主打成分與來源。完整 INCI 會另外標示；族群注意只採官方或權威來源。重複品、錯誤品名、版本不明與非單品不列入可研究缺漏。",
    }

def _persist_decision(canonical_key: str, decision: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        decisions = _load_decisions()
        decisions[canonical_key] = decision
        temporary = _DECISIONS_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(_DECISIONS_FILE)


def update_product_review(
    canonical_key: str,
    *,
    review_status: str,
    reviewer_notes: str = "",
    duplicate_of: str | None = None,
    reviewer_id_hash: str = "",
) -> dict[str, Any]:
    if review_status not in REVIEW_STATUSES:
        raise ValueError("Unsupported review status")
    products = {product["canonical_key"]: product for product in _load_products()}
    if canonical_key not in products:
        raise KeyError(canonical_key)
    if duplicate_of and duplicate_of not in products:
        raise ValueError("duplicate_of does not identify a review candidate")
    if duplicate_of == canonical_key:
        raise ValueError("A product cannot duplicate itself")

    product = products[canonical_key]
    current_sync_fingerprint = _product_sync_fingerprint(product)
    if product.get("import_status") == "imported":
        if review_status != "approved":
            raise ValueError("已匯入的商品不能從待審頁改為其他狀態")
        if product.get("sync_fingerprint") == current_sync_fingerprint:
            return product
        # Verified catalogue data changed after the first import. The Neo4j
        # writer is an idempotent upsert, so refresh only when the fingerprint differs.

    now = datetime.now(timezone.utc).isoformat()
    base_decision: dict[str, Any] = {
        "review_status": review_status,
        "reviewer_notes": reviewer_notes.strip()[:2000],
        "duplicate_of": duplicate_of or None,
        "reviewed_at": now,
        "reviewer_id_hash": reviewer_id_hash,
    }
    if review_status != "approved":
        decision = {
            **base_decision,
            "import_status": "not_requested",
            "imported_at": None,
            "import_error": "",
            "neo4j_sku": None,
            "neo4j_element_id": None,
            "recommendable": False,
        }
        _persist_decision(canonical_key, decision)
        return {**product, **decision}

    quality_blockers = _catalog_quality_blockers(product)
    if quality_blockers:
        raise ValueError("尚不能通過匯入：" + ", ".join(quality_blockers))

    try:
        import_result = _import_product_to_neo4j(
            product,
            reviewer_id_hash=reviewer_id_hash,
            imported_at=now,
        )
    except Exception as exc:
        safe_error = str(exc).strip()[:500] or type(exc).__name__
        decision = {
            **base_decision,
            "import_status": "failed",
            "imported_at": None,
            "import_error": safe_error,
            "neo4j_sku": _stable_review_sku(canonical_key),
            "neo4j_element_id": None,
            "recommendable": False,
        }
        _persist_decision(canonical_key, decision)
        failed_product = {**product, **decision}
        raise ProductImportError(
            f"審核已記錄，但匯入正式商品庫失敗：{safe_error}。請稍後直接重試。",
            product=failed_product,
        ) from exc

    decision = {
        **base_decision,
        **import_result,
        "sync_fingerprint": current_sync_fingerprint,
    }
    _persist_decision(canonical_key, decision)
    return {**product, **decision}
