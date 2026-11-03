"""Crawl curated trend/retailer pages into the product human-review queue.

The crawler never writes Neo4j. Firecrawl JSON extraction is constrained to the
curated source manifest, normalized, deduplicated against every seed, and saved
as pending candidates for an administrator to review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "product_review"
SOURCE_MANIFEST = DATA_DIR / "product_trend_sources_2024_2026.json"
OUTPUT_FILE = DATA_DIR / "product_catalog_crawled_candidates.json"
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
DEFAULT_SOURCE_IDS = (
    "atcosme_tw_2026_h1_newstar",
    "watsons_tw_2026_sunscreen_guide",
    "watsons_tw_2026_serum",
    "cmri_tw_2026_08",
    "elle_tw_2026_awards",
    "sephora_us_2026_bestselling",
)
FACE_TERMS = (
    "臉", "面", "精華", "化妝水", "乳液", "乳霜", "防曬", "面膜", "潔顏", "洗面",
    "serum", "toner", "moistur", "cream", "sunscreen", "spf", "mask", "cleanser", "facial",
    "cica", "centella", "積雪草", "老虎草", "舒緩", "鎮定", "bha", "exfoliant", "去角質", "水楊酸",
)
EXCLUDED_TERMS = (
    "洗髮", "護髮", "沐浴", "身體乳", "護手", "香水", "唇膏", "口紅", "粉底", "睫毛", "眼影",
    "shampoo", "conditioner", "body wash", "body lotion", "hand cream", "perfume", "lipstick",
    "foundation", "mascara", "supplement", "vitamin gummies", "body sunscreen", "妝前", "primer",
)
BRAND_ALIASES = {
    "loreal paris": "L'Oréal Paris 巴黎萊雅",
    "l'oréal paris": "L'Oréal Paris 巴黎萊雅",
    "巴黎萊雅": "L'Oréal Paris 巴黎萊雅",
    "my beauty diary": "我的美麗日記",
    "我的美麗日記": "我的美麗日記",
    "cerave": "CeraVe 適樂膚",
    "適樂膚": "CeraVe 適樂膚",
    "la roche-posay": "La Roche-Posay 理膚寶水",
    "la roche posay": "La Roche-Posay 理膚寶水",
    "理膚寶水": "La Roche-Posay 理膚寶水",
    "hada labo": "Hada-Labo 肌研",
    "肌研": "Hada-Labo 肌研",
    "mentholatum": "Mentholatum 曼秀雷敦",
    "曼秀雷敦": "Mentholatum 曼秀雷敦",
    "acnes": "Acnes 抗痘",
    "neutrogena": "Neutrogena 露得清",
    "露得清": "Neutrogena 露得清",
    "nivea": "NIVEA 妮維雅",
    "妮維雅": "NIVEA 妮維雅",
    "senka": "SENKA 專科",
    "專科": "SENKA 專科",
    "biore": "Bioré 蜜妮",
    "bioré": "Bioré 蜜妮",
    "蜜妮": "Bioré 蜜妮",
    "shu uemura": "shu uemura 植村秀",
    "植村秀": "shu uemura 植村秀",
    "mirae": "MIRAE 未來美",
    "未來美": "MIRAE 未來美",
    "purcent": "PURCENT 百分之百",
    "shiseido": "SHISEIDO 資生堂",
    "資生堂": "SHISEIDO 資生堂",
    "paula's choice": "Paula's Choice 寶拉珍選",
    "paulas choice": "Paula's Choice 寶拉珍選",
    "寶拉": "Paula's Choice 寶拉珍選",
    "寶拉珍選": "Paula's Choice 寶拉珍選",
    "cetaphil": "Cetaphil 舒特膚",
    "舒特膚": "Cetaphil 舒特膚",
    "舒特夫": "Cetaphil 舒特膚",
    "bio-essence": "Bio-Essence 碧歐斯",
    "bio essence": "Bio-Essence 碧歐斯",
    "bo essence": "Bio-Essence 碧歐斯",
    "碧歐斯": "Bio-Essence 碧歐斯",
    "ahc": "AHC 愛和純",
    "a.h.c": "AHC 愛和純",
    "愛和純": "AHC 愛和純",
    "vt": "VT COSMETICS",
    "vt cosmetics": "VT COSMETICS",
    "dr.jart+": "Dr.Jart+ 蒂佳婷",
    "dr jart": "Dr.Jart+ 蒂佳婷",
    "蒂佳婷": "Dr.Jart+ 蒂佳婷",
    "skin1004": "SKIN1004",
    "anua": "Anua",
    "round lab": "ROUND LAB",
    "roundlab": "ROUND LAB",
    "isntree": "Isntree 伊絲翠",
    "伊絲翠": "Isntree 伊絲翠",
    "curél": "Curél 珂潤",
    "curel": "Curél 珂潤",
    "珂潤": "Curél 珂潤",
    "elixir": "ELIXIR 怡麗絲爾",
    "怡麗絲爾": "ELIXIR 怡麗絲爾",
    "anessa": "ANESSA 安耐曬",
    "安耐曬": "ANESSA 安耐曬",
    "melano cc": "Melano CC",
    "樂敦cc": "Melano CC",
    "kracie": "Kracie 肌美精",
    "肌美精": "Kracie 肌美精",
    "minon": "MINON 蜜濃",
    "蜜濃": "MINON 蜜濃",
    "naturie": "Naturie",
    "薏仁水": "Naturie",
    "kose": "KOSE 高絲",
    "高絲": "KOSE 高絲",
    "雪肌精": "SEKKISEI 雪肌精",
}
SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string"},
                    "product_name": {"type": "string"},
                    "product_name_zh_official": {"type": "string"},
                    "product_name_original": {"type": "string"},
                    "common_names": {"type": "array", "items": {"type": "string"}},
                    "search_aliases": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string"},
                    "capacity": {"type": "string"},
                    "price": {"type": "number"},
                    "list_price": {"type": "number"},
                    "sale_price": {"type": "number"},
                    "currency": {"type": "string"},
                    "product_url": {"type": "string"},
                    "rank": {"type": "number"},
                    "key_claims": {"type": "array", "items": {"type": "string"}},
                    "ingredients": {"type": "array", "items": {"type": "string"}},
                    "ingredients_are_full_inci": {"type": "boolean"},
                    "official_highlight_ingredients": {"type": "array", "items": {"type": "string"}},
                    "claimed_functions": {"type": "array", "items": {"type": "string"}},
                    "ingredient_source_url": {"type": "string"},
                    "ingredient_source_scope": {"type": "string", "enum": ["exact_product_page", "listing_page", "series_page", "unknown"]},
                    "evidence_context": {"type": "string"},
                },
                "required": ["brand", "product_name", "category"],
            },
        }
    },
    "required": ["products"],
}
PROMPT = """Extract all named facial skincare products visible on this page, in page order.
Include cleansers, toners, facial essences/serums, face moisturizers/creams, face masks and face sunscreens.
Exclude makeup, hair care, body care, tools, supplements, oral medicines and products whose name is not actually visible.
For product_name, copy the complete visible listing name exactly. For product_name_original, copy the original-language name when shown.
For product_name_zh_official, copy only an explicitly displayed official Traditional Chinese product name. Never translate or invent a Chinese name.
Collect explicitly displayed nicknames or common names in common_names and useful visible name variants in search_aliases. Do not invent slang.
Capture capacity, list price, sale price, currency and the product detail URL when they are visible. Never infer a price from another product.
For official_highlight_ingredients and claimed_functions, copy only text explicitly stated for this exact product on an official brand product page. Put proprietary complexes or untranslated extracts in official_highlight_ingredients exactly as written; never expand them into guessed INCI.
Set ingredient_source_scope to exact_product_page only when the URL identifies one exact product. Series or listing pages are not product-specific ingredient evidence.
Use ingredients only when the page explicitly shows the complete ingredient/INCI list, and then set ingredients_are_full_inci=true. Otherwise leave ingredients empty.
Do not invent ingredients, prices, rankings, brands or URLs. Use empty arrays or omit optional fields when the page does not provide them.
For evidence_context, briefly state why the page lists the product (award, ranking, bestseller or editorial selection)."""


def _load(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = re.sub(r"\s+", " ", str(raw or "")).strip()
        key = item.casefold()
        if item and key not in seen:
            result.append(item[:240])
            seen.add(key)
    return result


def _normalized(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _contains_cjk(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(value or "")))


def _canonical_brand(value: Any) -> str:
    brand = re.sub(r"\s+", " ", str(value or "")).strip()[:160]
    return BRAND_ALIASES.get(brand.casefold(), brand)


def _first_positive(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _strip_capacity(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"\s*[\-–—,/]*(?:\d+(?:\.\d+)?\s*(?:ml|mL|g|克|毫升))(?:\s*[xX×]\s*\d+)?\s*$", "", text).strip(" -–—,/")


def _source_currency(source: dict[str, Any]) -> str:
    explicit = str(source.get("currency") or "").strip().upper()
    if explicit:
        return explicit[:12]
    source_id = str(source.get("source_id") or "").casefold()
    url = str(source.get("url") or "").casefold()
    if "_tw_" in source_id or ".tw" in url:
        return "TWD"
    if "sephora_us" in source_id:
        return "USD"
    return ""


def _canonical_category(value: Any, *names: Any) -> str:
    text = " ".join(str(item or "") for item in (value, *names)).casefold()
    if "面膜" in text or "mask" in text:
        return "面膜"
    if "防曬" in text or "sunscreen" in text or "spf" in text:
        return "臉部防曬"
    if any(term in text for term in ("洗面", "洗顏", "潔顏", "潔面", "cleanser", "face wash")):
        return "清潔／潔顏"
    if any(term in text for term in ("眼霜", "眼部", "眼周", "eye care", "eye cream")):
        return "眼周保養"
    if any(term in text for term in ("化妝水", "美膚水", "精華水", "toner", "mist", "噴霧")):
        return "化妝水／噴霧"
    if "精華油" in text or "face oil" in text or "facial oil" in text:
        return "精華油"
    if any(term in text for term in ("精華", "essence", "serum", "ampoule")):
        return "精華液"
    if any(term in text for term in ("乳液", "乳霜", "moisturizer", "moisturiser", "face cream", "facial cream")):
        return "乳液／乳霜"
    return "臉部保養"


def _brand_search_aliases(brand: str) -> list[str]:
    canonical = _canonical_brand(brand)
    return _clean_list([
        alias
        for alias, target in BRAND_ALIASES.items()
        if _normalized(target) == _normalized(canonical)
    ])


def _name_aliases(brand: str, official_zh: str, original: str, listing: str, raw_aliases: Any) -> list[str]:
    base = _clean_list([official_zh, original, listing, _strip_capacity(official_zh), _strip_capacity(original), _strip_capacity(listing), *_clean_list(raw_aliases)])
    additions: list[str] = []
    for name in base:
        for brand_alias in _brand_search_aliases(brand):
            if _normalized(brand_alias) not in _normalized(name):
                additions.append(f"{brand_alias} {name}")
    cica_text = _normalized(" ".join(base))
    if any(term in cica_text for term in ("cica", "積雪草", "centella")):
        additions.extend(["CICA", "積雪草", "老虎草", "Centella asiatica"])
    return _clean_list([*base, *additions])


def _candidate_key(brand: str, name: str) -> str:
    digest = hashlib.sha256(f"{_normalized(brand)}|{_normalized(name)}".encode("utf-8")).hexdigest()[:20]
    return f"crawl-{digest}"


def _is_face_product(product: dict[str, Any]) -> bool:
    text = " ".join(str(product.get(key) or "") for key in (
        "product_name", "product_name_zh_official", "product_name_original", "category", "evidence_context"
    )).casefold()
    if any(term in text for term in EXCLUDED_TERMS):
        return False
    return any(term in text for term in FACE_TERMS)


def _sources() -> dict[str, dict[str, Any]]:
    payload = _load(SOURCE_MANIFEST, {})
    return {
        str(source["source_id"]): source
        for source in payload.get("sources", [])
        if isinstance(source, dict) and source.get("source_id") and source.get("url")
    }


def _scrape(source: dict[str, Any], api_key: str) -> tuple[list[dict[str, Any]], int]:
    response = requests.post(
        FIRECRAWL_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "url": source["url"],
            "formats": [{"type": "json", "schema": SCHEMA, "prompt": PROMPT}],
            "onlyMainContent": True,
            "maxAge": 6 * 60 * 60 * 1000,
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError("Firecrawl did not return success")
    data = payload.get("data") or {}
    json_payload = data.get("json") or {}
    products = json_payload.get("products") if isinstance(json_payload, dict) else None
    if not isinstance(products, list):
        products = []
    credits = int(payload.get("creditsUsed") or 5)
    return [item for item in products if isinstance(item, dict)], credits


def _product_pair_keys(product: dict[str, Any]) -> set[str]:
    brand = _canonical_brand(product.get("canonical_brand") or product.get("brand"))
    names = _clean_list([
        product.get("taiwan_official_name"),
        product.get("official_name"),
        product.get("name_zh"),
        product.get("name_en"),
        product.get("original_name"),
        product.get("source_listing_name"),
        *_clean_list(product.get("common_names")),
        *_clean_list(product.get("search_aliases")),
    ])
    return {f"{_normalized(brand)}|{_normalized(name)}" for name in names if _normalized(name)}


def _known_pairs() -> set[str]:
    pairs: set[str] = set()
    for path in DATA_DIR.glob("product_catalog_*.json"):
        payload = _load(path, {})
        for product in payload.get("products", []) if isinstance(payload, dict) else []:
            if isinstance(product, dict):
                pairs.update(_product_pair_keys(product))
    return pairs


def _normalize_product(raw: dict[str, Any], source: dict[str, Any], position: int) -> dict[str, Any] | None:
    brand = _canonical_brand(raw.get("brand"))
    listing_name = re.sub(r"\s+", " ", str(raw.get("product_name") or "")).strip()[:240]
    original_name = re.sub(r"\s+", " ", str(raw.get("product_name_original") or listing_name)).strip()[:240]
    proposed_zh = re.sub(r"\s+", " ", str(raw.get("product_name_zh_official") or "")).strip()[:240]
    official_zh = proposed_zh if _contains_cjk(proposed_zh) else (listing_name if _contains_cjk(listing_name) else "")
    raw_category = re.sub(r"\s+", " ", str(raw.get("category") or "臉部保養")).strip()[:80]
    category = _canonical_category(raw_category, listing_name, proposed_zh, original_name)
    probe = {**raw, "category": f"{raw_category} {category}", "product_name": listing_name}
    if len(listing_name) < 2 or not brand or not _is_face_product(probe):
        return None

    captured_common_names = _clean_list(raw.get("common_names"))
    search_aliases = _name_aliases(
        brand,
        official_zh,
        original_name,
        listing_name,
        [*captured_common_names, *_clean_list(raw.get("search_aliases"))],
    )
    status = "source_verified_zh" if official_zh else "needs_official_chinese_name"
    blockers = [] if official_zh else ["missing_official_chinese_name"]
    display_name = official_zh or original_name or listing_name
    now = datetime.now(timezone.utc).isoformat()
    product_url = str(raw.get("product_url") or "").strip()[:1000]
    list_price = _first_positive(raw.get("list_price"), raw.get("price"))
    sale_price = _first_positive(raw.get("sale_price"), raw.get("price") if raw.get("list_price") is None else None)
    current_price = sale_price or list_price
    currency = str(raw.get("currency") or _source_currency(source)).strip().upper()[:12]
    evidence_context = re.sub(r"\s+", " ", str(raw.get("evidence_context") or "")).strip()[:300]

    result: dict[str, Any] = {
        "canonical_key": _candidate_key(brand, official_zh or original_name or listing_name),
        "brand": brand,
        "canonical_brand": brand,
        "name_zh": display_name,
        "taiwan_official_name": official_zh,
        "official_name": official_zh or original_name or listing_name,
        "original_name": original_name,
        "source_listing_name": listing_name,
        "common_names": captured_common_names,
        "search_aliases": search_aliases,
        "name_verification_status": status,
        "name_zh_status": status,
        "review_blockers": blockers,
        "category": category,
        "capacity": re.sub(r"\s+", " ", str(raw.get("capacity") or "")).strip()[:80],
        "cohort": source.get("time_bucket") or "recent",
        "concerns": _clean_list(raw.get("key_claims")),
        "ingredients_inci": (
            _clean_list(raw.get("ingredients"))
            if raw.get("ingredients_are_full_inci") is True
            else []
        ),
        "unmapped_official_highlights": _clean_list(
            raw.get("official_highlight_ingredients")
        ),
        "claimed_functions": _clean_list(raw.get("claimed_functions")),
        "ingredient_source_scope": str(raw.get("ingredient_source_scope") or "unknown"),
        "claim_evidence": ([{
            "source_name": str(source.get("name") or source.get("source_id") or "official product page"),
            "source_url": str(raw.get("ingredient_source_url") or product_url),
            "context": "；".join(_clean_list(raw.get("official_highlight_ingredients")) + _clean_list(raw.get("claimed_functions"))),
            "evidence_type": "official_claim",
        }] if (
            str(raw.get("ingredient_source_scope") or "") == "exact_product_page"
            and str(raw.get("ingredient_source_url") or product_url).strip()
            and _clean_list(raw.get("official_highlight_ingredients"))
        ) else []),
        "evidence": [{
            "source_id": source["source_id"],
            "rank": int(raw.get("rank") or position),
            "context": evidence_context or str(source.get("signal_type") or "商品榜單／熱門清單"),
        }],
        "crawl_product_url": product_url,
        "crawl_status": "pending_review",
        "crawled_at": now,
        "price_status": "observed" if current_price else "missing",
        "price_source_url": product_url or str(source.get("url") or ""),
        "price_observed_at": now if current_price else "",
    }
    if list_price:
        result["list_price"] = list_price
    if sale_price:
        result["sale_price"] = sale_price
    if current_price:
        result["price"] = current_price
        result["currency"] = currency
    return result


def _upgrade_existing_candidate(product: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(product, dict):
        return product
    evidence = product.get("evidence", []) if isinstance(product.get("evidence"), list) else []
    first_evidence = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    source_id = str(first_evidence.get("source_id") or "legacy_candidate")
    source = {"source_id": source_id, **sources.get(source_id, {})}
    listing = str(
        product.get("source_listing_name")
        or product.get("name_zh")
        or product.get("name_en")
        or ""
    ).strip()
    proposed_zh = str(product.get("taiwan_official_name") or "").strip()
    if not proposed_zh and _contains_cjk(product.get("name_zh")):
        proposed_zh = str(product.get("name_zh") or "").strip()
    raw = {
        "brand": product.get("canonical_brand") or product.get("brand"),
        "product_name": listing,
        "product_name_zh_official": proposed_zh,
        "product_name_original": product.get("original_name") or product.get("name_en") or listing,
        "common_names": product.get("common_names", []),
        "search_aliases": product.get("search_aliases", []),
        "category": product.get("category") or "臉部保養",
        "capacity": product.get("capacity"),
        "price": product.get("price"),
        "list_price": product.get("list_price"),
        "sale_price": product.get("sale_price"),
        "currency": product.get("currency"),
        "product_url": product.get("crawl_product_url"),
        "key_claims": product.get("concerns", []),
        "ingredients": product.get("ingredients_inci", []),
        "ingredients_are_full_inci": str(product.get("inci_status") or "") == "full",
        "official_highlight_ingredients": product.get("unmapped_official_highlights", []),
        "claimed_functions": product.get("claimed_functions", []),
        "ingredient_source_url": next((
            str(item.get("source_url") or "")
            for item in product.get("claim_evidence", [])
            if isinstance(item, dict) and str(item.get("source_url") or "").strip()
        ), ""),
        "ingredient_source_scope": (
            "exact_product_page" if product.get("claim_evidence") else "unknown"
        ),
        "evidence_context": first_evidence.get("context"),
        "rank": first_evidence.get("rank") or 1,
    }
    normalized = _normalize_product(raw, source, int(raw["rank"] or 1))
    if normalized is None:
        blockers = _clean_list([*product.get("review_blockers", []), "non_facial_product"])
        return {**product, "crawl_status": "excluded_non_facial", "review_blockers": blockers}
    return {**product, **normalized, "canonical_key": product.get("canonical_key") or normalized["canonical_key"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", action="append", dest="source_ids")
    parser.add_argument("--limit", type=int, default=len(DEFAULT_SOURCE_IDS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--normalize-only", action="store_true")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not args.normalize_only and not api_key.startswith("fc-"):
        print(json.dumps({"ok": False, "error": "FIRECRAWL_API_KEY is missing or invalid"}))
        return 2

    all_sources = _sources()
    requested = () if args.normalize_only else tuple(args.source_ids or DEFAULT_SOURCE_IDS)[: max(0, args.limit)]
    unknown = [source_id for source_id in requested if source_id not in all_sources]
    if unknown:
        print(json.dumps({"ok": False, "error": "unknown curated source", "source_ids": unknown}, ensure_ascii=False))
        return 2

    output = _load(OUTPUT_FILE, {"schema_version": "2.0", "sources": {}, "products": []})
    upgraded_existing = [
        _upgrade_existing_candidate(product, all_sources)
        for product in output.get("products", [])
        if isinstance(product, dict)
    ]
    output_products = {
        str(product.get("canonical_key")): product
        for product in upgraded_existing
        if product.get("canonical_key")
    }
    known_pairs = _known_pairs()
    results: list[dict[str, Any]] = []
    failures = 0
    total_credits = 0
    for source_id in requested:
        source = {"source_id": source_id, **all_sources[source_id]}
        started = time.perf_counter()
        try:
            raw_products, credits = _scrape(source, api_key)
            total_credits += credits
            accepted = 0
            skipped = 0
            for position, raw in enumerate(raw_products, 1):
                candidate = _normalize_product(raw, source, position)
                if candidate is None:
                    skipped += 1
                    continue
                pairs = _product_pair_keys(candidate)
                if pairs.intersection(known_pairs) and candidate["canonical_key"] not in output_products:
                    skipped += 1
                    continue
                output_products[candidate["canonical_key"]] = candidate
                known_pairs.update(pairs)
                accepted += 1
            output.setdefault("sources", {})[source_id] = source
            results.append({"source_id": source_id, "extracted": len(raw_products), "accepted": accepted, "skipped": skipped, "credits": credits, "seconds": round(time.perf_counter()-started, 2)})
        except Exception as exc:
            failures += 1
            results.append({"source_id": source_id, "error": str(exc)[:300], "seconds": round(time.perf_counter()-started, 2)})

    output.update({
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "review_only",
        "purpose": "Firecrawl candidates; never auto-publish",
        "products": sorted(output_products.values(), key=lambda item: (str(item.get("cohort")), str(item.get("brand")), str(item.get("name_zh")))),
    })
    if not args.dry_run:
        temporary = OUTPUT_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(OUTPUT_FILE)
    print(json.dumps({"ok": failures == 0, "dry_run": args.dry_run, "output": str(OUTPUT_FILE), "candidate_total": len(output_products), "credits_used": total_credits, "results": results}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
