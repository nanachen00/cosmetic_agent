"""Import authoritative skin-care pages as review-only daily-content drafts.

This worker deliberately never publishes content.  It stores evidence and a
short extracted preview so a BLINK administrator can review/edit it first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import requests
import time
from dotenv import load_dotenv


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from core.usage_tracking import record_usage, flush_usage
from database.postgres.connection import postgres_connection


FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
ALLOWED_DOMAINS = {
    "aad.org",
    "www.aad.org",
    "fda.gov",
    "www.fda.gov",
    "mohw.gov.tw",
    "www.mohw.gov.tw",
    "fda.gov.tw",
    "www.fda.gov.tw",
    "nhs.uk",
    "www.nhs.uk",
    "pmc.ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
}


@dataclass(frozen=True)
class Source:
    url: str
    content_type: str
    source_name: str


SOURCES = (
    Source("https://www.aad.org/public/everyday-care/skin-care-basics/dry/oily-skin", "beauty_tip", "American Academy of Dermatology"),
    Source("https://www.aad.org/public/everyday-care/skin-care-secrets/routine/facial-masks-and-skin-care", "beauty_tip", "American Academy of Dermatology"),
    Source("https://www.aad.org/public/diseases/acne/diy/adult-acne-treatment", "dermatologist_faq", "American Academy of Dermatology"),
    Source("https://www.fda.gov/cosmetics/cosmetic-ingredients/allergens-cosmetics", "dermatologist_faq", "U.S. Food and Drug Administration"),
    Source("https://www.fda.gov/cosmetics/resources-consumers-cosmetics/cosmetics-safety-qa-hypoallergenic", "beauty_tip", "U.S. Food and Drug Administration"),
    Source("https://www.fda.gov/cosmetics/labeling-claims/organic-cosmetics", "beauty_tip", "U.S. Food and Drug Administration"),
    
    Source("https://www.fda.gov/cosmetics/resources-consumers-cosmetics/using-cosmetics-safely", "dermatologist_faq", "U.S. Food and Drug Administration"),
    
    Source("https://www.fda.gov/safety/medical-product-safety-information/public-safety-alert-due-marketing-unapproved-stem-cell-and-exosome-products", "beauty_tip", "U.S. Food and Drug Administration"),
    Source("https://pubmed.ncbi.nlm.nih.gov/38633728/", "beauty_tip", "PubMed Central"),
    Source("https://pubmed.ncbi.nlm.nih.gov/40871045/", "beauty_tip", "PubMed Central"),
    Source("https://pubmed.ncbi.nlm.nih.gov/35038127/", "beauty_tip", "PubMed Central"),
    Source("https://pubmed.ncbi.nlm.nih.gov/40299368/", "beauty_tip", "PubMed Central"),
    Source("https://www.fda.gov/cosmetics/cosmetics-labeling/shelf-life-and-expiration-dating-cosmetics", "beauty_tip", "U.S. Food and Drug Administration"),
    Source("https://www.fda.gov/drugs/understanding-over-counter-medicines/sunscreen-how-help-protect-your-skin-sun", "beauty_tip", "U.S. Food and Drug Administration"),
    Source("https://www.nhs.uk/conditions/acne/causes/", "beauty_tip", "NHS"),
    Source("https://www.aad.org/public/diseases/acne/skin-care/tips", "dermatologist_faq", "American Academy of Dermatology"),
    Source("https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=152&mid=137", "beauty_tip", "Taiwan Food and Drug Administration"),
)


# The crawler preserves the complete official-language evidence, while these
# editorially reviewed zh-TW fields are what administrators see and publish.
# New sources without an override remain review-only and are never published.
CARD_OVERRIDES: dict[str, tuple[str, str]] = {
    "https://www.aad.org/public/everyday-care/skin-care-basics/dry/oily-skin": (
        "油性肌也需要保濕，過度清潔反而可能刺激肌膚",
        "皮膚科醫師建議每天洗臉不超過兩次並在流汗後清潔，選擇溫和、無油且不致粉刺的產品；油性肌仍需保濕，白天可搭配 SPF 30 以上廣效防曬。",
    ),
    "https://www.aad.org/public/everyday-care/skin-care-secrets/routine/facial-masks-and-skin-care": (
        "面膜是保養加分項，不能取代日常保養或治療",
        "依膚況選擇成分並先做局部測試；若出現搔癢、灼熱或刺痛應立即洗掉。面膜不能取代清潔、保濕、防曬，也不能取代痘痘或濕疹的治療。",
    ),
    "https://www.aad.org/public/diseases/acne/diy/adult-acne-treatment": (
        "成人痘保養別急著一直換產品",
        "先溫和清潔並選擇適合的抗痘成分，持續一段時間觀察，不要同時堆疊過多刺激性產品；若痘痘疼痛、留下疤痕或自行照護無改善，應尋求皮膚科醫師評估。",
    ),
    "https://www.fda.gov/cosmetics/cosmetic-ingredients/allergens-cosmetics": (
        "「敏感肌適用」不代表一定不會過敏",
        "化粧品常見過敏原包括香料、防腐劑、染料、金屬與天然橡膠。與其只看行銷標示，更重要的是閱讀完整成分表並避開自己已知的過敏原。",
    ),
    "https://www.fda.gov/cosmetics/resources-consumers-cosmetics/cosmetics-safety-qa-hypoallergenic": (
        "「低敏」不是零過敏保證",
        "美國 FDA 並沒有規定「低敏」必須通過特定測試或證明比其他產品更少引起過敏；有敏感或過敏史者仍應查看成分，並依產品指示先做局部測試。",
    ),
    "https://www.fda.gov/cosmetics/labeling-claims/organic-cosmetics": (
        "有機或天然化粧品不一定比較安全",
        "成分來源不等於安全性；天然或有機植物也可能含有刺激或致敏物。評估產品時仍要看完整配方、使用方式與自己的膚況。",
    ),
    "https://www.fda.gov/cosmetics/resources-consumers-cosmetics/using-cosmetics-safely": (
        "化粧品開封後，乾淨使用比囤貨更重要",
        "使用前先洗手、保持容器與工具清潔，且不要共用眼妝產品；若產品變色、變味或造成刺激就應停用，眼部感染時也不要繼續使用眼妝。",
    ),
    "https://www.fda.gov/cosmetics/cosmetics-labeling/shelf-life-and-expiration-dating-cosmetics": (
        "沒有標示效期，不代表化粧品可以一直用",
        "手指反覆沾取、浴室濕氣、高溫與日照都會加速產品劣化或微生物污染。可在容器標註開封日；若出現異味、變色、分層或質地改變就不要再使用。",
    ),
    "https://www.fda.gov/drugs/understanding-over-counter-medicines/sunscreen-how-help-protect-your-skin-sun": (
        "沒有真正「防水」的防曬，流汗游泳後仍要補擦",
        "防水不等於整天有效；標示耐水 40 或 80 分鐘，只代表測試條件下的耐水時間。日常仍應依標示補擦，游泳、流汗或擦拭後要更快補上。",
    ),
    "https://www.nhs.uk/conditions/acne/causes/": (
        "痘痘不是因為臉沒洗乾淨，牙膏也不是抗痘藥",
        "痘痘主要反應發生在皮膚內部，洗臉超過每天兩次反而可能刺激肌膚；牙膏中的某些成分也會刺激或傷害皮膚，不建議拿來點痘。",
    ),
    "https://www.aad.org/public/diseases/acne/skin-care/tips": (
        "抗痘產品一直換，可能讓肌膚更難穩定",
        "頻繁更換或同時嘗試多種抗痘產品容易造成刺激。皮膚科醫師建議給治療足夠時間，避免摳擠，並選擇標示不致粉刺的保養、防曬與彩妝。",
    ),
    "https://www.fda.gov/safety/medical-product-safety-information/public-safety-alert-due-marketing-unapproved-stem-cell-and-exosome-products": (
        "外泌體很熱門，但不能把保養品宣稱當成治療證據",
        "FDA 提醒，宣稱可治療疾病的外泌體產品需經上市前審查，目前沒有 FDA 核准的外泌體治療產品。這項警示不等同全面禁止外泌體化粧品，但平台不得把『外泌體』直接寫成已證實再生或治療。",
    ),
    "https://pubmed.ncbi.nlm.nih.gov/38633728/": (
        "外用外泌體有初步研究，但還不能只看成分名下結論",
        "系統性回顧中的人體研究數量仍有限，而且不少研究同時搭配微針或能量療程，難以分離外泌體本身的效果；來源、製程、劑量與安全資料仍需標準化。",
    ),
    "https://pubmed.ncbi.nlm.nih.gov/40871045/": (
        "PDRN、PN 與外用保養不是同一件事",
        "PDRN 與 PN 的分子長度及用途不同，外用保養常以 Sodium DNA 或 Hydrolyzed DNA 出現在成分表；注射或醫療研究結果不能直接套用到精華液，仍要看濃度、載體與人體試驗。",
    ),
    "https://pubmed.ncbi.nlm.nih.gov/35038127/": (
        "依克多因有人體資料，但配方濃度很重要",
        "一篇系統性回顧納入六項研究，其中外用配方約含 5.5% 至 7% 依克多因並觀察乾燥與皮膚屏障相關結果；不能把這些結果直接推論到所有僅標示 Ectoin 的低濃度化粧品。",
    ),
    "https://pubmed.ncbi.nlm.nih.gov/40299368/": (
        "後生元不是單一成分，不能只看行銷名詞",
        "後生元可能與皮膚微生態及屏障保養有關，但不同微生物來源、代謝物、劑量與配方差異很大；標示『後生元』不代表每款產品效果相同，仍需產品層級證據。",
    ),
    "https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=152&mid=137": (
        "只擦美白產品不防曬，美白功夫可能白費",
        "想改善膚色不均，除了依標示正確使用美白化粧品，也要把防曬納入日常。避免把多效合一等行銷文字，直接當成適合所有膚況的保證。",
    ),
}


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOMAINS:
        raise ValueError(f"Source is not on the approved HTTPS allowlist: {url}")


def _clean_markdown(markdown: str) -> str:
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", markdown)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`#>*_|~-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _preview(markdown: str, limit: int = 360) -> str:
    text = _clean_markdown(markdown)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def _crawl(source: Source, api_key: str) -> dict[str, Any]:
    _validate_url(source.url)
    started = time.perf_counter()
    try:
        response = requests.post(
            FIRECRAWL_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": source.url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=75,
        )
        response.raise_for_status()
        payload = response.json()
        credits = payload.get("creditsUsed") or payload.get("credits_used") or 1
        record_usage(category="crawler", provider="firecrawl", service="scrape-v2",
                     operation="scrape", quantity=float(credits), unit_type="credit",
                     latency_ms=(time.perf_counter()-started)*1000,
                     metadata={"source_name": source.source_name})
    except Exception as exc:
        record_usage(category="crawler", provider="firecrawl", service="scrape-v2",
                     operation="scrape", status="error", quantity=1, unit_type="request",
                     latency_ms=(time.perf_counter()-started)*1000,
                     error_type=type(exc).__name__, metadata={"source_name": source.source_name})
        flush_usage()
        raise
    if not payload.get("success"):
        raise RuntimeError(f"Firecrawl did not return success for {source.url}")
    data = payload.get("data") or {}
    markdown = str(data.get("markdown") or "").strip()
    if len(markdown) < 200:
        raise RuntimeError(f"Firecrawl returned too little content for {source.url}")
    metadata = data.get("metadata") or {}
    return {"markdown": markdown, "metadata": metadata}


def _store(source: Source, crawled: dict[str, Any], dry_run: bool) -> str:
    markdown = crawled["markdown"]
    metadata = crawled["metadata"]
    page_title = str(metadata.get("title") or source.url.rstrip("/").split("/")[-1]).strip()[:240]
    localized = CARD_OVERRIDES.get(source.url)
    title = localized[0] if localized else page_title
    summary = localized[1] if localized else _preview(markdown)
    # One review draft per approved source. Page chrome can change between
    # crawls even when the medical guidance did not, so URL is the stable key.
    content_hash = hashlib.sha256(source.url.encode("utf-8")).hexdigest()
    stored_metadata = {
        "pipeline": "firecrawl_v2_review_queue",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "page_title": page_title,
        "description": metadata.get("description"),
        "source_language": metadata.get("language"),
        "card_locale": "zh-TW" if localized else "source-language",
        "localization_status": "editorial_override" if localized else "needs_translation",
        "evidence_excerpt": _clean_markdown(markdown)[:1200],
        "requires_human_review": True,
    }
    if dry_run:
        return "dry_run"
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, review_status FROM daily_contents
                WHERE source_kind = 'crawler' AND source_url = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source.url,),
            )
            existing = cursor.fetchone()
            if existing and existing["review_status"] in {"pending_review", "in_review"}:
                cursor.execute(
                    """
                    UPDATE daily_contents
                    SET content_type = %s, title = %s, summary = %s,
                        source_name = %s, content_hash = %s, metadata = %s::jsonb,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        source.content_type,
                        title,
                        summary,
                        source.source_name,
                        content_hash,
                        json.dumps(stored_metadata, ensure_ascii=False),
                        existing["id"],
                    ),
                )
                connection.commit()
                return "updated"
            if existing:
                # Published/rejected/archived sources are editorial decisions. A
                # future crawl must not recreate them as a fresh pending card.
                return "unchanged"
            cursor.execute(
                """
                INSERT INTO daily_contents (
                    content_type, title, summary, source_kind, source_name,
                    source_url, review_status, is_active, display_priority,
                    content_hash, metadata
                ) VALUES (%s, %s, %s, 'crawler', %s, %s,
                          'pending_review', FALSE, 0, %s, %s::jsonb)
                ON CONFLICT (content_hash) DO NOTHING
                RETURNING id
                """,
                (
                    source.content_type,
                    title,
                    summary,
                    source.source_name,
                    source.url,
                    content_hash,
                    json.dumps(stored_metadata, ensure_ascii=False),
                ),
            )
            inserted = cursor.fetchone()
        connection.commit()
    return "inserted" if inserted else "unchanged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=len(SOURCES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", help="Crawl one allowlisted URL instead of the source catalog")
    parser.add_argument("--content-type", choices=("beauty_tip", "dermatologist_faq"), default="beauty_tip")
    args = parser.parse_args()

    load_dotenv(".env", override=False)
    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not api_key.startswith("fc-"):
        print(json.dumps({"ok": False, "error": "FIRECRAWL_API_KEY is missing or invalid"}))
        return 2

    sources = (
        (Source(args.url, args.content_type, urlparse(args.url).hostname or "Official source"),)
        if args.url
        else SOURCES[: max(0, args.limit)]
    )
    results: list[dict[str, str]] = []
    failures = 0
    for source in sources:
        try:
            result = _store(source, _crawl(source, api_key), args.dry_run)
            results.append({"url": source.url, "result": result})
        except Exception as exc:  # Continue other approved sources; scheduler gets non-zero exit.
            failures += 1
            results.append({"url": source.url, "result": "failed", "error": str(exc)[:300]})
    print(json.dumps({"ok": failures == 0, "review_status": "pending_review", "results": results}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
