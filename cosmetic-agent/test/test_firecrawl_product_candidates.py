from scripts import firecrawl_product_candidates as crawler


def _source():
    return {
        "source_id": "watsons_tw_2026_serum",
        "time_bucket": "recent",
        "signal_type": "retailer_bestselling",
        "url": "https://www.watsons.com.tw/example",
    }


def test_keeps_official_chinese_original_name_and_search_aliases():
    result = crawler._normalize_product(
        {
            "brand": "La Roche-Posay",
            "product_name": "MELA B3 淡斑淨亮精華 30ml",
            "product_name_zh_official": "MELA B3 淡斑淨亮精華 30ml",
            "product_name_original": "Mela B3 Serum",
            "common_names": ["MELA B3 精華"],
            "search_aliases": ["理膚寶水 B3 精華"],
            "category": "精華液",
        },
        _source(),
        1,
    )
    assert result is not None
    assert result["brand"] == "La Roche-Posay 理膚寶水"
    assert result["taiwan_official_name"] == "MELA B3 淡斑淨亮精華 30ml"
    assert result["original_name"] == "Mela B3 Serum"
    assert result["name_verification_status"] == "source_verified_zh"
    assert "MELA B3 精華" in result["common_names"]
    assert "理膚寶水 B3 精華" in result["search_aliases"]
    assert "MELA B3 淡斑淨亮精華" in result["search_aliases"]


def test_english_only_product_waits_for_official_chinese_name():
    result = crawler._normalize_product(
        {
            "brand": "CeraVe",
            "product_name": "Hydrating Hyaluronic Acid Serum",
            "category": "face serum",
        },
        _source(),
        1,
    )
    assert result is not None
    assert result["taiwan_official_name"] == ""
    assert result["name_verification_status"] == "needs_official_chinese_name"
    assert result["review_blockers"] == ["missing_official_chinese_name"]
    assert result["name_zh"] == "Hydrating Hyaluronic Acid Serum"


def test_prices_keep_sale_list_source_and_observed_time():
    result = crawler._normalize_product(
        {
            "brand": "肌研",
            "product_name": "白潤高效集中淡斑精華",
            "category": "精華液",
            "list_price": 520,
            "sale_price": 399,
            "currency": "twd",
            "capacity": "30g",
            "product_url": "https://www.watsons.com.tw/p/123",
        },
        _source(),
        1,
    )
    assert result is not None
    assert result["price"] == 399.0
    assert result["list_price"] == 520.0
    assert result["sale_price"] == 399.0
    assert result["currency"] == "TWD"
    assert result["capacity"] == "30g"
    assert result["price_source_url"].endswith("/p/123")
    assert result["price_status"] == "observed"
    assert result["price_observed_at"]


def test_zero_price_is_missing():
    result = crawler._normalize_product(
        {
            "brand": "Brand",
            "product_name": "Hydrating Face Serum",
            "category": "face serum",
            "price": 0,
        },
        _source(),
        1,
    )
    assert result is not None
    assert result["crawl_status"] == "pending_review"
    assert result["price_status"] == "missing"
    assert "price" not in result


def test_excludes_body_sunscreen_and_makeup_primer():
    body = crawler._normalize_product(
        {"brand": "Brand", "product_name": "Daily Sunscreen", "category": "body sunscreen"},
        _source(),
        1,
    )
    primer = crawler._normalize_product(
        {"brand": "Brand", "product_name": "妝前防曬乳", "category": "妝前"},
        _source(),
        2,
    )
    assert body is None
    assert primer is None


def test_normalizes_search_category_names():
    result = crawler._normalize_product(
        {"brand": "Brand", "product_name": "Daily Face Sunscreen", "category": "face sunscreen"},
        _source(),
        1,
    )
    assert result is not None
    assert result["category"] == "臉部防曬"


def test_upgrade_marks_non_facial_candidate_excluded():
    product = {
        "canonical_key": "legacy-primer",
        "brand": "Brand",
        "name_zh": "潤色妝前乳",
        "category": "隔離妝前",
        "evidence": [{"source_id": "watsons_tw_2026_serum", "rank": 1}],
    }
    result = crawler._upgrade_existing_candidate(
        product,
        {"watsons_tw_2026_serum": _source()},
    )
    assert result["canonical_key"] == "legacy-primer"
    assert result["crawl_status"] == "excluded_non_facial"
    assert "non_facial_product" in result["review_blockers"]
