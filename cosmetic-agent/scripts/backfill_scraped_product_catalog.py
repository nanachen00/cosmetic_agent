"""Audit and safely enrich legacy Neo4j products with source_type=scraped.

The default mode is read-only. ``--apply`` is required for every database
write.  The script never changes source_type or recommendable, and it only
stores a source URL when a high-confidence retailer-domain match is found.
Official product pages may then be parsed with Firecrawl; inferred or absent
ingredients are never written as verified formula evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from neo4j import GraphDatabase


GENERIC_CATEGORIES = {"", "臉部保養", "身體保養", "其他", "unknown"}
SOURCE_DOMAINS = {
    "watsons": ("watsons.com.tw",),
    "cosmed": ("cosmed.com.tw",),
    "poya": ("poyabuy.com.tw",),
    "oliveyoung": ("oliveyoung.com",),
}
CATEGORY_RULES = (
    # Final product form wins over marketing ingredient/texture words.
    ("護唇", ("護唇", "潤唇", "唇膜", "lip balm", "lip treatment")),
    ("面膜", ("面膜", "凍膜", "泥膜", "sheet mask", "mask pack")),
    ("防曬乳", ("防曬", "隔離乳", "sunscreen", "sun cream", "sun serum", "spf")),
    ("保濕棉片", ("棉片", "toner pad", "toning pad")),
    ("洗面乳", ("洗面", "潔面", "潔顏", "洗顏", "cleanser", "face wash", "cleansing foam")),
    ("化妝水", ("化妝水", "爽膚水", "精華水", "調理水", "保濕噴霧", "toner", "skin lotion", "mist")),
    ("精華液", ("精華", "安瓶", "serum", "ampoule", "essence")),
    ("乳霜", ("乳霜", "面霜", "凝霜", "cream", "balm")),
    ("乳液", ("乳液", "凝乳", "lotion", "emulsion", "moisturizer", "moisturiser")),
)
NON_SKINCARE_CATEGORY_TERMS = ("定妝", "眼罩", "setting spray", "eye mask")
SKINCARE_CATEGORIES = {
    "臉部保養",
    "洗面乳",
    "卸妝",
    "化妝水",
    "保濕棉片",
    "精華液",
    "乳液",
    "乳霜",
    "眼霜",
    "面膜",
    "防曬乳",
    "痘痘貼",
    "護唇",
}
SKINCARE_NAME_TERMS = (
    "洗面",
    "潔面",
    "潔顏",
    "洗顏",
    "卸妝",
    "化妝水",
    "爽膚水",
    "調理水",
    "保濕噴霧",
    "棉片",
    "精華",
    "安瓶",
    "乳液",
    "乳霜",
    "面霜",
    "眼霜",
    "面膜",
    "凍膜",
    "泥膜",
    "防曬",
    "痘痘貼",
    "去角質",
    "煥膚",
    "護唇",
    "唇膜",
    "cleanser",
    "toner",
    "serum",
    "ampoule",
    "essence",
    "emulsion",
    "moisturizer",
    "cream",
    "sunscreen",
    "sun cream",
    "face mask",
)
NON_SKINCARE_PRODUCT_TERMS = (
    "定妝",
    "粉底",
    "粉餅",
    "蜜粉",
    "遮瑕",
    "腮紅",
    "眼影",
    "眼線",
    "眉筆",
    "睫毛膏",
    "唇釉",
    "口紅",
    "香水",
    "洗髮",
    "護髮",
    "沐浴",
    "牙膏",
    "漱口",
    "衛生棉",
    "眼罩",
    "美甲",
    "指甲油",
    "刮痧板",
    "按摩器",
    "美容儀",
    "按摩滾輪",
    "洗臉巾",
    "棉巾",
    "免洗毛巾",
)
GENERIC_HIGHLIGHT_CLAIMS = {
    "水",
    "保濕",
    "美白",
    "滋潤",
    "潤澤",
    "舒緩",
    "修護",
    "緊緻",
    "抗老",
    "提亮",
    "控油",
    "清潔",
}
FIRECRAWL_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "brand": {"type": "string"},
        "category": {"type": "string"},
        "description": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "ingredients_are_full_inci": {"type": "boolean"},
        "official_highlight_ingredients": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["product_name", "brand", "category", "description", "ingredients", "ingredients_are_full_inci", "official_highlight_ingredients"],
}
FIRECRAWL_PROMPT = """Read this exact retailer product page only. Extract the displayed product name, brand, product category and a short factual description. Put ingredients in ingredients only when the page displays a complete INCI/full ingredient list for this exact product, and set ingredients_are_full_inci=true only then. Put explicitly named hero or highlighted skincare ingredients in official_highlight_ingredients. Exclude scent, fragrance and flavor descriptions (for example floral scent or peach scent). Never infer ingredients from the product name, claims, category, similar products or general brand knowledge. Return empty arrays when evidence is absent."""


def clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        item = re.sub(r"\s+", " ", str(raw or "")).strip()
        if item and item.casefold() not in {x.casefold() for x in result}:
            result.append(item[:240])
    return result


def clean_highlight_ingredients(value: Any) -> list[str]:
    result: list[str] = []
    for item in clean_list(value):
        normalized = re.sub(r"\s+", "", item).casefold()
        if any(term in normalized for term in ("香調", "香氛", "香味", "香氣")):
            continue
        if normalized.endswith(("花香", "果香", "桃香")):
            continue
        if normalized in GENERIC_HIGHLIGHT_CLAIMS:
            continue
        result.append(item)
    return result


def infer_category(name: str) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).casefold()
    if any(term in text for term in NON_SKINCARE_CATEGORY_TERMS):
        return ""
    for category, terms in CATEGORY_RULES:
        if any(term in text for term in terms):
            return category
    return ""


def is_skincare_candidate(product: dict[str, Any]) -> bool:
    name = re.sub(r"\s+", " ", str(product.get("name") or "")).casefold()
    category = str(product.get("category") or "").strip()
    for term in NON_SKINCARE_PRODUCT_TERMS:
        if term not in name:
            continue
        if term == "定妝" and "防曬" in name:
            continue
        return False
    return category in SKINCARE_CATEGORIES or any(term in name for term in SKINCARE_NAME_TERMS)


def normalized_name(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"(?:\d+(?:\.\d+)?\s*(?:ml|g|克|毫升)|\d+\s*(?:入|片|枚|瓶|包))", " ", text)
    text = re.sub(r"(?:limited|edition|set|組|限定|增量版|超值包|買一送一|awards?)", " ", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def allowed_domain(url: str, source: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == domain or host.endswith("." + domain) for domain in SOURCE_DOMAINS.get(source, ()))


def candidate_score(product: dict[str, Any], result: dict[str, Any]) -> float:
    url = str(result.get("url") or "").strip()
    if not allowed_domain(url, str(product.get("source") or "")):
        return 0.0
    path = urlparse(url).path.strip("/").casefold()
    if not path or any(marker in path for marker in ("search", "category", "brand-list", "collections/all")):
        return 0.0
    expected = normalized_name(product.get("name") or "")
    title = normalized_name(result.get("title") or "")
    if len(expected) < 5 or len(title) < 5:
        return 0.0
    similarity = SequenceMatcher(None, expected, title).ratio()
    containment = min(len(expected), len(title)) / max(len(expected), len(title)) if expected in title or title in expected else 0.0
    brand = normalized_name(product.get("brand") or "")
    brand_bonus = 0.08 if brand and brand in title else 0.0
    return max(similarity, containment) + brand_bonus


def discover_url(product: dict[str, Any], api_key: str) -> tuple[str, float]:
    source = str(product.get("source") or "")
    domains = SOURCE_DOMAINS.get(source)
    if not domains:
        return "", 0.0
    query = " ".join(part for part in (product.get("brand"), product.get("name")) if str(part or "").strip())
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": 5,
            "include_answer": False,
            "include_raw_content": False,
            "include_domains": list(domains),
        },
        timeout=45,
    )
    response.raise_for_status()
    ranked = sorted(
        ((candidate_score(product, item), str(item.get("url") or "").strip()) for item in response.json().get("results", []) if isinstance(item, dict)),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.72:
        return "", ranked[0][0] if ranked else 0.0
    return ranked[0][1], ranked[0][0]


def scrape_details(url: str, api_key: str) -> dict[str, Any]:
    response = requests.post(
        "https://api.firecrawl.dev/v2/scrape",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "url": url,
            "formats": [{"type": "json", "schema": FIRECRAWL_SCHEMA, "prompt": FIRECRAWL_PROMPT}],
            "onlyMainContent": True,
            "maxAge": 24 * 60 * 60 * 1000,
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError("Firecrawl did not return success")
    data = (payload.get("data") or {}).get("json") or {}
    return data if isinstance(data, dict) else {}


def audit(session) -> dict[str, Any]:
    query = """
    MATCH (p:Product) WHERE p.source_type = 'scraped'
    RETURN count(*) AS total,
      sum(CASE WHEN trim(coalesce(p.source_url,'')) <> '' OR size(coalesce(p.source_urls,[])) > 0 THEN 1 ELSE 0 END) AS has_source_url,
      sum(CASE WHEN size(coalesce(p.ingredients_list,[])) > 0 OR size(coalesce(p.key_ingredients,[])) > 0 THEN 1 ELSE 0 END) AS has_ingredients,
      sum(CASE WHEN trim(coalesce(p.category,'')) <> '' AND NOT p.category IN $generic THEN 1 ELSE 0 END) AS specific_category
    """
    return dict(session.run(query, generic=sorted(GENERIC_CATEGORIES)).single())


def cleanup_existing(session, source: str | None, apply: bool) -> dict[str, int]:
    where = ["p.enrichment_pipeline = 'scraped_product_backfill_v1'"]
    params: dict[str, Any] = {}
    if source:
        where.append("p.source = $source")
        params["source"] = source
    rows = session.run(
        f"""
        MATCH (p:Product) WHERE {' AND '.join(where)}
        RETURN elementId(p) AS id, p.name AS name, p.category AS category,
               p.category_evidence AS category_evidence,
               coalesce(p.key_ingredients, []) AS key_ingredients
        """,
        **params,
    )
    stats = {"ingredient_rows_cleaned": 0, "categories_corrected": 0}
    for row in rows:
        product = dict(row)
        original_highlights = clean_list(product.get("key_ingredients"))
        cleaned_highlights = clean_highlight_ingredients(original_highlights)
        if cleaned_highlights != original_highlights:
            stats["ingredient_rows_cleaned"] += 1
            if apply and cleaned_highlights:
                session.run(
                    "MATCH (p:Product) WHERE elementId(p)=$id SET p.key_ingredients=$items",
                    id=product["id"],
                    items=cleaned_highlights,
                ).consume()
            elif apply:
                session.run(
                    """
                    MATCH (p:Product) WHERE elementId(p)=$id
                    REMOVE p.key_ingredients, p.ingredient_evidence_level, p.ingredient_source_url
                    """,
                    id=product["id"],
                ).consume()

        category_evidence = str(product.get("category_evidence") or "")
        if category_evidence not in {"official_product_page", "deterministic_name_rule"}:
            continue
        current_category = str(product.get("category") or "")
        inferred_category = infer_category(product.get("name") or "")
        if not is_skincare_candidate(product):
            category_target = ""
        elif inferred_category:
            category_target = inferred_category
        else:
            continue
        if category_target == current_category:
            continue
        stats["categories_corrected"] += 1
        if apply and category_target:
            session.run(
                """
                MATCH (p:Product) WHERE elementId(p)=$id
                SET p.category=$category, p.category_evidence='deterministic_name_rule'
                """,
                id=product["id"],
                category=category_target,
            ).consume()
        elif apply:
            session.run(
                """
                MATCH (p:Product) WHERE elementId(p)=$id
                REMOVE p.category, p.category_evidence
                """,
                id=product["id"],
            ).consume()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=sorted(SOURCE_DOMAINS))
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--discover-urls", action="store_true")
    parser.add_argument("--extract-details", action="store_true")
    parser.add_argument(
        "--cleanup-existing",
        action="store_true",
        help="Revalidate fields previously written by this enrichment pipeline.",
    )
    parser.add_argument(
        "--skincare-only",
        action="store_true",
        help="Prioritize facial skincare and exclude obvious makeup, hair, body and accessory products.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    load_dotenv(".env", override=False)
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if args.discover_urls and not tavily_key:
        raise SystemExit("TAVILY_API_KEY is required for --discover-urls")
    if args.extract_details and not firecrawl_key:
        raise SystemExit("FIRECRAWL_API_KEY is required for --extract-details")

    where = ["p.source_type = 'scraped'"]
    requested_limit = max(0, args.limit)
    query_limit = requested_limit * 20 if args.skincare_only else requested_limit
    params: dict[str, Any] = {"limit": query_limit}
    if args.source:
        where.append("p.source = $source")
        params["source"] = args.source
    query = f"""
    MATCH (p:Product) WHERE {' AND '.join(where)}
    RETURN elementId(p) AS id, p.name AS name, p.brand AS brand,
           p.source AS source, p.source_url AS source_url,
           coalesce(p.source_urls, []) AS source_urls,
           p.category AS category, coalesce(p.ingredients_list, []) AS ingredients_list,
           coalesce(p.key_ingredients, []) AS key_ingredients
    ORDER BY CASE WHEN trim(coalesce(p.source_url,'')) = '' AND size(coalesce(p.source_urls,[])) = 0 THEN 0 ELSE 1 END,
             p.source, p.name
    LIMIT $limit
    """
    stats = {"category_updates": 0, "url_matches": 0, "detail_updates": 0, "errors": 0}
    examples: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    with driver.session() as session:
        before = audit(session)
        cleanup_stats = (
            cleanup_existing(session, args.source, args.apply)
            if args.cleanup_existing
            else {"ingredient_rows_cleaned": 0, "categories_corrected": 0}
        )
        stats.update(cleanup_stats)
        products = [dict(row) for row in session.run(query, **params)]
        if args.skincare_only:
            products = [product for product in products if is_skincare_candidate(product)]
        products = products[:requested_limit]
        for product in products:
            changes: dict[str, Any] = {}
            inferred = infer_category(product.get("name") or "")
            if str(product.get("category") or "") in GENERIC_CATEGORIES and inferred:
                changes.update(category=inferred, category_evidence="deterministic_name_rule")

            urls = [str(value).strip() for value in product.get("source_urls", []) if str(value).startswith(("http://", "https://"))]
            source_url = str(product.get("source_url") or "").strip()
            if not source_url and urls:
                source_url = urls[0]
                changes["source_url"] = source_url
            if not source_url and args.discover_urls:
                try:
                    source_url, score = discover_url(product, tavily_key)
                    if source_url:
                        changes.update(source_url=source_url, source_urls=[source_url], source_match_score=round(score, 4))
                        stats["url_matches"] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    if len(examples) < 12:
                        examples.append({"name": product.get("name"), "error": str(exc)[:160]})

            if source_url and args.extract_details:
                try:
                    details = scrape_details(source_url, firecrawl_key)
                    full_inci = clean_list(details.get("ingredients")) if details.get("ingredients_are_full_inci") is True else []
                    highlights = clean_highlight_ingredients(details.get("official_highlight_ingredients"))
                    if full_inci:
                        changes.update(
                            ingredients_list=full_inci,
                            ingredient_evidence_level="full_inci",
                            ingredient_source_url=source_url,
                        )
                    if highlights:
                        changes.update(
                            key_ingredients=highlights,
                            ingredient_evidence_level=("full_inci" if full_inci else "official_highlights"),
                            ingredient_source_url=source_url,
                        )
                    description = re.sub(r"\s+", " ", str(details.get("description") or "")).strip()
                    if description:
                        changes["description"] = description[:600]
                    detail_product_name = str(details.get("product_name") or product.get("name") or "")
                    detail_category = infer_category(detail_product_name) or infer_category(
                        str(details.get("category") or "")
                    )
                    if detail_category:
                        changes.update(category=detail_category, category_evidence="official_product_page")
                    if full_inci or highlights or description:
                        stats["detail_updates"] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    if len(examples) < 12:
                        examples.append({"name": product.get("name"), "url": source_url, "error": str(exc)[:160]})

            if "category" in changes:
                stats["category_updates"] += 1
            if changes and len(examples) < 12:
                examples.append({"name": product.get("name"), "source": product.get("source"), "changes": changes})
            if changes and args.apply:
                changes.update(enrichment_updated_at=now, enrichment_pipeline="scraped_product_backfill_v1")
                session.run(
                    "MATCH (p:Product) WHERE elementId(p)=$id SET p += $changes",
                    id=product["id"],
                    changes=changes,
                ).consume()
            if args.discover_urls or args.extract_details:
                time.sleep(max(0.0, args.sleep))
        after = audit(session)
    driver.close()
    print(json.dumps({"ok": stats["errors"] == 0, "apply": args.apply, "selected": len(products), "before": before, "after": after, "stats": stats, "examples": examples}, ensure_ascii=False, indent=2))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
