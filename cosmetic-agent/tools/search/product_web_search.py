"""Small, source-preserving web fallback for named cosmetic products.

This tool is intentionally not part of normal catalogue ranking.  It runs only
for a named product/brand detail request, or after an explicit brand constraint
returns no local catalogue rows.
"""

from __future__ import annotations

import copy
import json
import ipaddress
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from openai import OpenAI
import requests
from bs4 import BeautifulSoup


try:
    from core.usage_tracking import record_usage
except Exception:  # Usage tracking must never break the user request.
    def record_usage(**_: Any) -> None:
        return None


_TAVILY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DETAIL_FIELD_TERMS: dict[str, tuple[str, ...]] = {
    "benefits": ("功能", "功效", "作用", "好用嗎", "評價"),
    "skin_types": ("膚質", "乾肌", "油肌", "混合肌", "敏感肌", "痘痘肌"),
    "ingredients": ("成分", "配方"),
    "usage": ("怎麼用", "如何使用", "用法", "多久", "頻率"),
    "warnings": ("注意", "刺激", "安全", "副作用", "過敏"),
    "price": ("價格", "多少錢", "售價"),
}


def infer_product_detail_fields(query: str) -> list[str]:
    """Return the smallest evidence field set needed by the current question."""
    normalized = str(query or "").casefold()
    fields = [
        field
        for field, terms in _DETAIL_FIELD_TERMS.items()
        if any(term.casefold() in normalized for term in terms)
    ]
    return fields or ["summary"]




def _extract_explicit_skin_types(text: object) -> list[str]:
    """Extract only skin types that the source explicitly marks as suitable."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []
    if any(term in raw for term in ("所有膚質", "全膚質", "任何膚質", "各種膚質")):
        return ["所有膚質"]

    mappings = (
        ("敏感肌", ("敏感肌", "敏弱肌", "敏感性肌膚")),
        ("乾性肌膚", ("乾肌", "乾性肌", "乾性肌膚")),
        ("油性肌膚", ("油肌", "油性肌", "油性肌膚")),
        ("混合性肌膚", ("混合肌", "混合性肌", "混合性肌膚")),
        ("中性肌膚", ("中性肌", "中性肌膚")),
    )
    suitability_cues = ("適合", "適用", "推薦", "建議", "可使用", "皆可", "均可")
    negative_cues = ("沒有適用", "未標示適用", "不代表適合", "無法判斷適合", "並非適合")
    found: list[str] = []
    for label, terms in mappings:
        for term in terms:
            for match in re.finditer(re.escape(term), raw):
                window = raw[max(0, match.start() - 36): min(len(raw), match.end() + 36)]
                if (
                    any(cue in window for cue in suitability_cues)
                    and not any(cue in window for cue in negative_cues)
                ):
                    found.append(label)
                    break
            if label in found:
                break
    return found


def _source_kind(url: str, brand_hint: str = "") -> str:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    brand_token = re.sub(r"[^a-z0-9]", "", str(brand_hint or "").casefold())
    host_token = re.sub(r"[^a-z0-9]", "", host)
    if brand_token and brand_token in host_token:
        return "official"
    if any(token in host for token in ("shopee", "momo", "pchome", "watsons", "cosmed")):
        return "retailer"
    if any(token in host for token in ("blog", "medium", "dcard", "ptt")):
        return "editorial"
    return "other"


def build_product_evidence_packet(
    *,
    query: str,
    products: list[dict[str, Any]],
    provider: str,
    requested_fields: list[str] | None = None,
    fallback_reason: str = "",
    request_id: str = "",
    duration_ms: int = 0,
    cache_hit: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    """Normalize local/web product facts into the only contract Terra consumes."""
    items: list[dict[str, Any]] = []
    for product in (products or [])[:3]:
        source_rows: list[Any] = list(product.get("sources") or [])
        raw_urls = product.get("source_urls") or []
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        for url in [product.get("source_url"), product.get("reference_url"), *raw_urls]:
            if url:
                source_rows.append({"title": "資料來源", "url": url})
        sources: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for source in source_rows:
            if isinstance(source, str):
                source = {"url": source}
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append({
                "title": str(source.get("title") or source.get("name") or "資料來源").strip(),
                "url": url,
                "domain": (urlparse(url).hostname or "").casefold(),
                "source_type": str(source.get("source_type") or _source_kind(url, str(product.get("brand") or ""))),
            })
        items.append({
            "product_id": str(product.get("id") or product.get("product_id") or ""),
            "name": str(product.get("name") or "").strip(),
            "brand": str(product.get("brand") or "").strip(),
            "category": str(product.get("category") or "").strip(),
            "skin_types": product.get("skin_types") or product.get("suitable_skin_types") or [],
            "description": str(
                product.get("description") or product.get("summary") or product.get("copy") or ""
            ).strip(),
            "ingredients": product.get("ingredients_list") or product.get("ingredients") or [],
            "key_ingredients": product.get("key_ingredients") or [],
            "concerns": product.get("concerns") or [],
            "functional_categories": product.get("functional_categories") or [],
            "usage": product.get("usage") or product.get("directions") or "",
            "warnings": product.get("warnings") or product.get("precautions") or [],
            "price": product.get("price"),
            "currency": product.get("currency") or "",
            "sources": sources[:5],
        })
    packet_status = status or ("ok" if items else "not_found")
    return {
        "schema_version": "1.0",
        "tool": "product_evidence_lookup",
        "provider": str(provider or "unknown"),
        "status": packet_status,
        "reason": str(fallback_reason or ""),
        "requested_fields": list(requested_fields or infer_product_detail_fields(query)),
        "query_label": str(query or "").strip(),
        "request": {
            "provider_request_id": str(request_id or ""),
            "duration_ms": max(0, int(duration_ms or 0)),
            "cache_hit": bool(cache_hit),
        },
        "items": items,
    }


DETAIL_MARKERS = (
    "好用嗎", "評價", "心得", "成分", "功效", "作用", "怎麼用",
    "適合嗎", "安全嗎", "會刺激嗎", "會過敏嗎", "多少錢", "價格",
    "哪裡買", "通路", "新款",
    "適合什麼膚質", "適合哪種膚質", "適合何種膚質",
    "適合敏感肌嗎", "適合乾肌嗎", "適合油肌嗎", "適合混合肌嗎",
)


TREND_MARKERS = (
    "最新", "流行", "熱門", "趨勢", "人氣", "熱銷", "爆款", "口碑榜",
    "有名", "知名", "大牌", "主流品牌",
)


def is_trending_product_query(value: Any) -> bool:
    """Identify trend intent without turning generic web pages into products."""
    query = str(getattr(value, "raw_query", value) or "").strip().casefold()
    return any(marker in query for marker in TREND_MARKERS)

def should_search_named_product(intent) -> bool:
    """Allow web lookup only for a resolved, named product detail request."""
    query = str(getattr(intent, "raw_query", "") or "").strip()
    brands = [
        str(value).strip()
        for value in (getattr(intent, "brand_preference", None) or [])
        if str(value).strip()
    ]
    if is_trending_product_query(intent):
        return False
    if not any(marker in query for marker in DETAIL_MARKERS):
        return False
    if brands:
        return True
    reference_terms = ("這款", "這個商品", "這個產品", "這瓶", "這罐", "它", "剛剛那款", "上一款")
    if any(term in query for term in reference_terms):
        return False
    product_hints = ("精華", "乳液", "乳霜", "化妝水", "洗面", "防曬", "面膜", "潔顏", "眼霜", "凝膠")
    return len(query) >= 12 and any(hint in query for hint in product_hints)


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("web search response did not contain JSON")
    return json.loads(text[start:end])


def _extract_sources(value: Any, limit: int = 5) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(item: Any) -> None:
        if len(sources) >= limit:
            return
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                normalized = url.strip()
                if normalized not in seen:
                    seen.add(normalized)
                    sources.append({
                        "title": str(item.get("title") or "網頁來源").strip(),
                        "url": normalized,
                    })
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sources


def _stable_web_id(name: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", name.casefold()).strip("-")
    return f"web:{normalized[:80]}"


def _is_safe_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".local") or "." not in hostname:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


def _clean_product_title(title: str, brand_hint: str = "") -> str:
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if "推薦：" in value:
        value = value.split("推薦：", 1)[1]
    value = value.split(" | ", 1)[0].strip(" -|'\"")

    if brand_hint:
        value = re.sub(
            rf"^\s*[\[【(（][^\]】)）]*{re.escape(brand_hint)}[^\]】)）]*[\]】)）]\s*",
            "",
            value,
            flags=re.I,
        )
        for separator in ("，", ","):
            head, found, tail = value.partition(separator)
            if found and brand_hint.casefold() in head.casefold():
                value = tail.strip()
                break
        value = re.sub(
            rf"\s*[-｜|]\s*{re.escape(brand_hint)}\s*$",
            "",
            value,
            flags=re.I,
        )
        value = re.sub(
            rf"^\s*{re.escape(brand_hint)}\s*[|:：,，\-–—]*\s*",
            "",
            value,
            flags=re.I,
        )
    return value[:140].strip(" -｜|")



_ARTICLE_TITLE_MARKERS = (
    "top " , "best " , "推薦", "排行", "評比", "清單", "懶人包",
    "guide", "review", "reviews", "比較",
)


_NON_PRODUCT_TITLE_MARKERS = ("活動", "專區")
_NON_PRODUCT_URL_MARKERS = (
    "/event/", "/events/", "/article/", "/articles/",
    "/blog/", "/blogs/", "/news/", "/campaign/",
    "/category/", "/categories/", "/collection/", "/collections/",
    "/product/list/", "/search",
)
_GENERIC_PRODUCT_TITLES = {
    "修復霜", "乳霜", "精華", "精華液", "化妝水", "乳液",
    "面膜", "防曬", "防曬乳", "洗面乳", "潔面", "眼霜",
    "凝露", "保濕", "護膚", "產品", "商品",
}


def _looks_like_article_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    return (
        any(marker in normalized for marker in _ARTICLE_TITLE_MARKERS)
        or any(marker in normalized for marker in _NON_PRODUCT_TITLE_MARKERS)
    )


def _looks_like_non_product_result(
    title: str,
    source_url: str,
    brand_hint: str = "",
) -> bool:
    """Reject category, campaign, and other pages that are not product details."""
    parsed_url = urlparse(source_url)
    path = (parsed_url.path or "").casefold()
    if any(marker in path for marker in _NON_PRODUCT_URL_MARKERS):
        return True
    segments = {segment for segment in path.split("/") if segment}
    detail_segments = {"product", "products", "item", "items", "goods", "shop", "p", "dp"}
    query = (parsed_url.query or "").casefold()
    has_detail_query = any(marker in query for marker in ("sku=", "pid=", "product_id=", "item_id="))
    if not (segments & detail_segments) and not has_detail_query:
        return True
    cleaned_title = _clean_product_title(title, brand_hint)
    normalized_title = re.sub(r"[\W_]+", "", cleaned_title, flags=re.UNICODE).casefold()
    return normalized_title in _GENERIC_PRODUCT_TITLES


def _infer_category(name: str, fallback: str | None = None) -> str:
    mapping = (
        ("化妝水", ("化妝水", "爽膚水", "機能水")),
        ("洗面乳", ("洗面", "潔面", "潔顏", "洗顏")),
        ("精華液", ("精華", "安瓶")),
        ("面膜", ("面膜", "泥膜", "凍膜")),
        ("防曬乳", ("防曬",)),
        ("乳液", ("乳液", "乳霜", "面霜", "凝乳")),
    )
    for category, keywords in mapping:
        if any(keyword in name for keyword in keywords):
            return category
    return fallback or "其他"


_WEB_CATEGORY_KEYWORDS = {
    "洗面乳": ("洗面", "潔面", "潔顏", "洗顏", "洗卸"),
    "化妝水": ("化妝水", "化粧水", "爽膚水", "機能水", "精華水"),
    "精華液": ("精華", "安瓶"),
    "面膜": ("面膜", "凍膜", "泥膜"),
    "防曬乳": ("防曬", "隔離"),
    "乳液": ("乳液", "乳霜", "面霜", "凝乳", "凝霜", "保濕乳", "修護乳", "修護霜"),
    "乳霜": ("乳霜", "面霜", "乳液", "凝霜", "凝乳", "保濕霜", "修護霜"),
    "保濕棉片": ("棉片",),
}


def _matches_requested_category(item, category) -> bool:
    """品類是使用者的硬性條件：web 結果不符品類一律不得輸出。"""
    if not category or str(category).strip() in {"", "不明", "未指定"}:
        return True
    keywords = _WEB_CATEGORY_KEYWORDS.get(str(category).strip())
    if not keywords:
        return True
    name = str((item or {}).get("name") or "")
    stated = str((item or {}).get("category") or "")
    if any(keyword in name for keyword in keywords):
        return True
    if any(keyword in stated for keyword in keywords):
        return True
    return str(category).strip() in stated


def _product_from_source_pages(
    sources: list[dict[str, str]],
    *,
    brand_hint: str = "",
    category: str | None = None,
    search_terms: list[str] | None = None,
    excluded_urls: set[str] | None = None,
) -> dict[str, Any] | None:
    """Read public page metadata only; never execute or follow page instructions."""
    ranked_sources = sorted(
        sources,
        key=lambda item: (
            0 if "/product/content" in item.get("url", "").casefold()
            else 1 if "/product/" in item.get("url", "").casefold()
            else 2
        ),
    )
    for source in ranked_sources[:5]:
        url = str(source.get("url") or "").strip()
        detail_name_hint = ""
        if url in (excluded_urls or set()) or not _is_safe_public_url(url):
            continue
        try:
            response = requests.get(
                url,
                timeout=12,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ProductLookup/1.0)"},
            )
            if response.status_code != 200 or len(response.content) > 2_000_000:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            # Search results sometimes expose a category/list page instead of
            # a product page.  Follow exactly one same-site product detail link,
            # preferring anchor text that matches the requested concern/category.
            if "/product/content" not in url.casefold():
                terms = [
                    str(term).strip().casefold()
                    for term in (search_terms or []) + ([category] if category else [])
                    if str(term).strip()
                ]
                if "舒敏" in terms:
                    terms.extend(["舒緩", "敏感"])
                detail_candidates = []
                for anchor in soup.find_all("a", href=True):
                    href = str(anchor.get("href") or "")
                    if "/product/content" not in href.casefold():
                        continue
                    text = anchor.get_text(" ", strip=True)
                    if not text:
                        continue
                    detail_url = urljoin(url, href)
                    if urlparse(detail_url).hostname != urlparse(url).hostname:
                        continue
                    normalized_text = text.casefold()
                    score = sum(term in normalized_text for term in terms)
                    if not category and any(
                        term in normalized_text
                        for term in ("卸妝", "洗面", "潔膚", "潔顏")
                    ):
                        score -= 1
                    detail_candidates.append((score, text, detail_url))
                if detail_candidates:
                    detail_candidates.sort(key=lambda item: (-item[0], item[1]))
                    detail_name_hint = detail_candidates[0][1]
                    detail_url = detail_candidates[0][2]
                    detail_response = requests.get(
                        detail_url,
                        timeout=12,
                        allow_redirects=False,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; ProductLookup/1.0)"
                        },
                    )
                    if (
                        detail_response.status_code == 200
                        and len(detail_response.content) <= 2_000_000
                    ):
                        url = detail_url
                        response = detail_response
                        soup = BeautifulSoup(response.text, "html.parser")
            title_tag = soup.find("meta", attrs={"property": "og:title"})
            raw_title = (
                title_tag.get("content")
                if title_tag and title_tag.get("content")
                else soup.title.get_text(" ", strip=True) if soup.title else ""
            )
            raw_title = raw_title or detail_name_hint
            name = _clean_product_title(raw_title, brand_hint=brand_hint)
            if not name:
                continue
            description_tag = (
                soup.find("meta", attrs={"property": "og:description"})
                or soup.find("meta", attrs={"name": "description"})
            )
            page_description = str(
                description_tag.get("content")
                if description_tag and description_tag.get("content")
                else ""
            ).strip()
            page_description = re.sub(r"\s+", " ", page_description)
            source["title"] = raw_title[:120] or source.get("title") or "官方商品頁"
            source["url"] = url
            sources.remove(source)
            sources.insert(0, source)
            return {
                "name": name,
                "brand": brand_hint or "未標示",
                "category": _infer_category(name, category),
                "price": None,
                "currency": "TWD",
                "summary": page_description[:180] or f"官方頁面列有「{name}」，可查看完整商品資訊。",
                "source_url": url,
            }
        except (requests.RequestException, ValueError) as error:
            print(f"[ProductWebSearch] source metadata failed for {url}: {error}")
            continue
    return None


def _fallback_product_from_text(
    text: str,
    *,
    brand_hint: str = "",
    category: str | None = None,
) -> dict[str, Any] | None:
    """Recover one cited result when the search model returns prose, not JSON."""
    normalized = str(text or "").strip()
    if not normalized:
        return None
    bold_match = re.search(r"\*\*([^*]{2,120})\*\*", normalized)
    label_match = re.search(
        r"(?:商品名稱|產品名稱)\s*[：:]\s*([^\n]{2,120})",
        normalized,
    )
    name = (
        bold_match.group(1)
        if bold_match
        else label_match.group(1) if label_match else ""
    ).strip(" *。")
    if not name:
        return None
    summary = re.sub(r"\s+", " ", normalized)
    return {
        "name": name,
        "brand": brand_hint or "未標示",
        "category": category or "其他",
        "price": None,
        "summary": summary[:180],
    }


def search_named_product(
    query: str,
    *,
    skin_type: str = "unknown",
    concerns: list[str] | None = None,
    category: str | None = None,
    brand_hint: str = "",
    max_results: int = 3,
    client: OpenAI | None = None,
    requested_fields: list[str] | None = None,
    fallback_reason: str = "local_catalogue_miss",
) -> dict:
    """Search the live web and return up to three concise, cited candidates."""
    if client is None and os.getenv("TAVILY_API_KEY", "").strip():
        return _search_named_product_tavily(
            query,
            skin_type=skin_type,
            concerns=concerns,
            category=category,
            brand_hint=brand_hint,
            max_results=max_results,
            requested_fields=requested_fields,
            fallback_reason=fallback_reason,
        )

    max_results = max(1, min(int(max_results or 3), 3))
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if client is None and not api_key:
        return {
            "products": [],
            "warnings": [],
            "source": "web",
            "status": "unavailable",
            "message": "目前無法使用網路商品搜尋。",
        }

    client = client or OpenAI(
        api_key=api_key,
        timeout=float(os.getenv("PRODUCT_WEB_SEARCH_TIMEOUT_SECONDS", "45")),
        max_retries=1,
    )
    model = os.getenv("PRODUCT_WEB_SEARCH_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    concern_text = "、".join(concerns or []) or "未指定"
    category_text = category if category and category != "不明" else "未指定"
    if category_text != "未指定" and category_text not in query:
        query = f"{query} {category_text}"
    category_rule = (
        "品類是硬性條件：每一筆都必須是此品類的商品，其他品類一律不得列入。"
        if category_text != "未指定"
        else ""
    )
    prompt = (
        f"搜尋台灣官方品牌頁：{query}。"
        f"膚質 {skin_type}；需求 {concern_text}；品類 {category_text}。{category_rule}"
        f"選出 {max_results} 個最相關且彼此不同的真實商品。"
        "每一筆必須是不同商品的官方商品詳情頁；禁止使用品牌首頁、分類頁或重複商品。"
        "只可使用官方品牌頁或官方商城頁的資訊，不採用論壇、內容農場或搜尋摘要。"
        "依每個來源頁的商品資訊，生成一段 45 到 80 字的繁體中文簡短介紹，"
        "並自然說明它與使用者需求的關聯；不要使用 emoji、直線分隔符或制式 AI 開場。"
        "不得猜測未明載的價格、成分或功效。"
        "不得使用治療、醫治、消炎、殺菌、抗菌、醫師推薦或消除痘痘等宣稱。"
        "只輸出 JSON 物件，格式為："
        '{"products":[{"name":"商品名稱","brand":"品牌","category":"品類",'
        '"price":null,"currency":"TWD","summary":"來源導向的簡短介紹",'
        '"source_url":"該商品唯一的官方網址"}]}。'
    )

    try:
        response = client.responses.create(
            model=model,
            tools=[{
                "type": "web_search",
                "search_context_size": "medium",
            }],
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=prompt,
            max_output_tokens=int(os.getenv("PRODUCT_WEB_SEARCH_MAX_TOKENS", "2400")),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "external_product_recommendations",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "products": {
                                "type": "array",
                                "minItems": max_results,
                                "maxItems": max_results,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "brand": {"type": "string"},
                                        "category": {"type": "string"},
                                        "price": {"type": ["number", "null"]},
                                        "currency": {"type": "string"},
                                        "summary": {"type": "string"},
                                        "source_url": {"type": "string"},
                                    },
                                    "required": [
                                        "name", "brand", "category", "price",
                                        "currency", "summary", "source_url",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["products"],
                        "additionalProperties": False,
                    },
                },
            },
            store=False,
        )
        try:
            parsed = _extract_json(response.output_text or "")
        except (ValueError, json.JSONDecodeError):
            fallback = _fallback_product_from_text(
                response.output_text or "",
                brand_hint=brand_hint,
                category=category,
            )
            parsed = {"products": [fallback] if fallback else []}
        response_data = (
            response.model_dump()
            if hasattr(response, "model_dump")
            else {}
        )
        sources = _extract_sources(response_data)
        print(
            "[ProductWebSearch] "
            f"text_chars={len(response.output_text or '')}, sources={len(sources)}"
        )
        existing = [
            item for item in (parsed.get("products") or [])
            if isinstance(item, dict)
            and _matches_requested_category(item, category)
        ]
        if not existing and sources:
            source_product = _product_from_source_pages(
                sources,
                brand_hint=brand_hint,
                category=category,
                search_terms=concerns,
            )
            if source_product and _matches_requested_category(
                source_product, category
            ):
                existing.append(source_product)

        # Some web-search models return one cited result even when asked for a
        # JSON list.  Run bounded follow-up searches instead of turning brand
        # homepages or duplicate URLs into extra recommendations.
        for _attempt in range(max_results * 2):
            if len(existing) >= max_results:
                break
            excluded_names = [
                str(item.get("name") or "").strip()
                for item in existing
                if str(item.get("name") or "").strip()
            ]
            followup_prompt = (
                f"{prompt}\n這次只找一個新的官方商品詳情頁。"
                f"不要重複以下商品：{'、'.join(excluded_names) or '無'}。"
                "不要回傳品牌首頁或分類頁。"
            )
            followup_response = client.responses.create(
                model=model,
                tools=[{
                    "type": "web_search",
                    "search_context_size": "medium",
                }],
                tool_choice="auto",
                include=["web_search_call.action.sources"],
                input=followup_prompt,
                max_output_tokens=int(os.getenv("PRODUCT_WEB_SEARCH_FOLLOWUP_MAX_TOKENS", "900")),
                store=False,
            )
            followup_data = (
                followup_response.model_dump()
                if hasattr(followup_response, "model_dump")
                else {}
            )
            followup_sources = _extract_sources(followup_data)
            source_product = _product_from_source_pages(
                followup_sources,
                brand_hint=brand_hint,
                category=category,
                search_terms=concerns,
            )
            if not source_product:
                continue
            if not _matches_requested_category(source_product, category):
                continue
            candidate_name = re.sub(
                r"\W+", "", str(source_product.get("name") or "").casefold()
            )
            candidate_slug = urlparse(
                str(source_product.get("source_url") or "")
            ).path.rstrip("/").rsplit("/", 1)[-1].casefold()
            existing_names = {
                re.sub(r"\W+", "", str(item.get("name") or "").casefold())
                for item in existing
            }
            existing_slugs = {
                urlparse(str(item.get("source_url") or ""))
                .path.rstrip("/").rsplit("/", 1)[-1].casefold()
                for item in existing
            }
            if candidate_name in existing_names or candidate_slug in existing_slugs:
                continue
            existing.append(source_product)
            for source in followup_sources:
                if source not in sources:
                    sources.append(source)
        parsed = {"products": existing}
        products = []
        seen_names: set[str] = set()
        seen_urls: set[str] = set()
        seen_product_slugs: set[str] = set()
        source_by_url = {
            str(source.get("url") or "").strip(): source
            for source in sources
            if isinstance(source, dict)
        }
        unused_sources = [source for source in sources if isinstance(source, dict)]
        for item in (parsed.get("products") or [])[:max_results]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if not name or not summary or not sources:
                continue
            if not _matches_requested_category(item, category):
                continue
            normalized_name = re.sub(r"\W+", "", name.casefold())
            summary = re.sub(r"[|｜]+", "，", summary)
            summary = re.sub(
                "[\U0001F300-\U0001FAFF\U00002600-\U000027BF]",
                "",
                summary,
            )
            summary = re.sub(r"\s+", " ", summary).strip(" ，")
            requested_url = str(item.get("source_url") or "").strip()
            selected_source = source_by_url.get(requested_url)
            if not requested_url and unused_sources:
                selected_source = unused_sources[0]
            if selected_source is None:
                continue
            selected_url = str(selected_source.get("url") or "").strip()
            selected_path = urlparse(selected_url).path.strip("/")
            if not selected_path:
                continue
            product_slug = selected_path.rsplit("/", 1)[-1].casefold()
            if (
                normalized_name in seen_names
                or selected_url in seen_urls
                or product_slug in seen_product_slugs
            ):
                continue
            if selected_source in unused_sources:
                unused_sources.remove(selected_source)
            seen_names.add(normalized_name)
            seen_urls.add(selected_url)
            seen_product_slugs.add(product_slug)
            price = item.get("price")
            if not isinstance(price, (int, float)):
                price = None
            currency = str(item.get("currency") or "TWD").strip().upper()
            if price is not None and currency == "USD":
                price = int(round(price * float(os.getenv("PRODUCT_USD_TWD_RATE", "31.855"))))
            elif price is not None and currency != "TWD":
                price = None
            products.append({
                "id": _stable_web_id(name),
                "name": name,
                "brand": str(item.get("brand") or "未標示").strip(),
                "category": str(item.get("category") or "其他").strip(),
                "price": price,
                "currency": "TWD",
                "copy": summary[:90].rstrip("，、；： ") + ("" if summary[:90].endswith(("。", "！", "？")) else "。"),
                "recommendation_reason": "本地商品庫沒有符合項目，改查最新網頁資料",
                "is_partner": False,
                "is_web_result": True,
                "reference_url": selected_url,
                "sources": [selected_source],
            })
        return {
            "products": products,
            "warnings": [],
            "graph_summary": "",
            "total_candidates": len(products),
            "source": "web",
            "status": "success" if products else "not_found",
            "message": "" if products else "網路上也找不到足夠可信的商品資料。",
        }
    except Exception as error:
        print(f"[ProductWebSearch] 搜尋失敗：{error}")
        return {
            "products": [],
            "warnings": [],
            "graph_summary": "",
            "total_candidates": 0,
            "source": "web",
            "status": "unavailable",
            "message": "目前無法取得可信的網路商品資料，請稍後再試。",
        }
def _search_named_product_tavily(
    query: str,
    *,
    skin_type: str,
    concerns: list[str] | None,
    category: str | None,
    brand_hint: str,
    max_results: int,
    requested_fields: list[str] | None = None,
    fallback_reason: str = "local_catalogue_miss",
) -> dict[str, Any]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    fields = list(requested_fields or infer_product_detail_fields(query))
    if not api_key:
        return {
            "products": [], "warnings": [], "source": "tavily",
            "status": "unavailable", "message": "TAVILY_API_KEY is not set.",
            "evidence_packet": build_product_evidence_packet(
                query=query, products=[], provider="tavily",
                requested_fields=fields, fallback_reason=fallback_reason,
            ),
        }

    field_hints = {
        "benefits": "功效",
        "skin_types": "適用膚質",
        "ingredients": "成分",
        "usage": "使用方法",
        "warnings": "注意事項",
        "price": "價格",
    }
    parts = [
        str(query or "").strip(),
        str(brand_hint or "").strip(),
        str(category or "").strip(),
        " ".join(str(item).strip() for item in (concerns or []) if str(item).strip()),
        "官方商品",
        " ".join(field_hints[field] for field in fields if field in field_hints),
    ]
    search_query = " ".join(part for part in parts if part).strip()
    if not search_query:
        return {
            "products": [], "warnings": [], "source": "tavily",
            "status": "unavailable", "message": "Search query is empty.",
            "evidence_packet": build_product_evidence_packet(
                query=query, products=[], provider="tavily",
                requested_fields=fields, fallback_reason=fallback_reason,
            ),
        }

    requested_results = max(1, min(int(max_results or 3), 3))
    cache_key = json.dumps(
        [search_query.casefold(), fields, requested_results],
        ensure_ascii=False,
        sort_keys=True,
    )
    now = time.monotonic()
    ttl_seconds = max(60, int(os.getenv("TAVILY_CACHE_TTL_SECONDS", "3600")))
    cached = _TAVILY_CACHE.get(cache_key)
    if cached and now - cached[0] <= ttl_seconds:
        result = copy.deepcopy(cached[1])
        result["evidence_packet"]["request"]["cache_hit"] = True
        record_usage(
            category="crawler", provider="tavily", service="search-cache",
            operation="product_detail_lookup", status="cache_hit",
            quantity=0, unit_type="request", latency_ms=0,
            metadata={"fallback_reason": fallback_reason, "fields": ",".join(fields)},
        )
        return result

    fetch_results = min(4, max(3, requested_results + 1))
    timeout_seconds = max(
        2.0,
        min(float(os.getenv("PRODUCT_WEB_SEARCH_TIMEOUT_SECONDS", "8")), 12.0),
    )
    started = time.perf_counter()
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": search_query,
                "search_depth": os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
                "topic": "general",
                "max_results": fetch_results,
                "include_answer": False,
                "include_raw_content": True,
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as error:
        duration_ms = int((time.perf_counter() - started) * 1000)
        record_usage(
            category="crawler", provider="tavily", service="search-basic",
            operation="product_detail_lookup", status="error",
            quantity=1, unit_type="request", latency_ms=duration_ms,
            error_type=type(error).__name__,
            metadata={"fallback_reason": fallback_reason, "fields": ",".join(fields)},
        )
        return {
            "products": [], "warnings": [], "source": "tavily",
            "status": "unavailable", "message": f"Tavily search failed: {error}",
            "evidence_packet": build_product_evidence_packet(
                query=query, products=[], provider="tavily",
                requested_fields=fields, fallback_reason=fallback_reason,
                duration_ms=duration_ms,
            ),
        }

    products: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("url") or "").strip()
        if not source_url or not _is_safe_public_url(source_url):
            continue
        normalized_url = source_url.rstrip("/")
        if normalized_url in seen_urls:
            continue
        title = str(item.get("title") or "").strip()
        raw_content = re.sub(
            r"\s+", " ", str(item.get("raw_content") or item.get("content") or "")
        ).strip()
        if _looks_like_article_title(title):
            continue
        if _looks_like_non_product_result(title, source_url, brand_hint):
            continue
        if brand_hint and brand_hint.casefold() not in (title + " " + raw_content).casefold():
            continue
        name = _clean_product_title(title, brand_hint)
        if not name:
            continue
        normalized_name = re.sub(r"\W+", "", name.casefold())
        if normalized_name in seen_names:
            continue
        summary = raw_content[:2400] or "請開啟來源頁查看商品詳情。"
        source = {"title": title or name, "url": source_url}
        products.append({
            "id": _stable_web_id(name),
            "name": name,
            "brand": brand_hint or "未標示",
            "category": _infer_category(name, category) or category or "其他",
            "price": None,
            "currency": "TWD",
            "summary": summary,
            "description": summary,
            "skin_types": _extract_explicit_skin_types(raw_content),
            "source_url": source_url,
            "copy": summary,
            "recommendation_reason": "本地資料庫缺少所問欄位，以公開來源補充",
            "is_partner": False,
            "is_web_result": True,
            "reference_url": source_url,
            "sources": [source],
        })
        seen_urls.add(normalized_url)
        seen_names.add(normalized_name)

    products.sort(
        key=lambda product: {
            "official": 0, "retailer": 1, "editorial": 2, "other": 3,
        }.get(
            _source_kind(
                str(product.get("source_url") or ""),
                str(product.get("brand") or brand_hint),
            ),
            3,
        )
    )
    products = products[:requested_results]
    duration_ms = int((time.perf_counter() - started) * 1000)
    provider_request_id = str(
        payload.get("request_id")
        or (getattr(response, "headers", {}) or {}).get("x-request-id")
        or ""
    ).strip() or None
    evidence_packet = build_product_evidence_packet(
        query=query,
        products=products,
        provider="tavily",
        requested_fields=fields,
        fallback_reason=fallback_reason,
        request_id=provider_request_id,
        duration_ms=duration_ms,
    )
    result = {
        "products": products,
        "sources": [source for product in products for source in product.get("sources") or []],
        "warnings": [],
        "source": "tavily",
        "status": "ok" if products else "not_found",
        "message": "" if products else "Tavily returned no usable product evidence.",
        "evidence_packet": evidence_packet,
    }
    source_domains = sorted({
        (urlparse(str(source.get("url") or "")).hostname or "").casefold()
        for source in result["sources"]
        if source.get("url")
    })
    record_usage(
        category="crawler", provider="tavily", service="search-basic",
        operation="product_detail_lookup",
        status="success" if products else "no_match",
        quantity=1, unit_type="request", latency_ms=duration_ms,
        metadata={
            "fallback_reason": fallback_reason,
            "fields": ",".join(fields),
            "result_count": len(products),
            "source_domains": ",".join(source_domains[:5]),
            "provider_request_id": provider_request_id or "",
        },
    )
    _TAVILY_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(result))
    return result
