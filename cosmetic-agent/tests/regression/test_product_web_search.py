from tools.search.product_web_search import build_product_evidence_packet
from types import SimpleNamespace

from tools.search.product_web_search import (
    _clean_product_title,
    _fallback_product_from_text,
    _extract_sources,
    _extract_explicit_skin_types,
    _search_named_product_tavily,
    is_trending_product_query,
    should_search_named_product,
    search_named_product,
)


def test_named_brand_detail_uses_web_search():
    intent = SimpleNamespace(
        raw_query="理膚寶水 B5 修復霜好用嗎",
        brand_preference=["理膚寶水"],
    )
    assert should_search_named_product(intent) is True


def test_generic_ingredient_question_does_not_use_product_web_search():
    intent = SimpleNamespace(
        raw_query="玻尿酸是什麼",
        brand_preference=[],
    )
    assert should_search_named_product(intent) is False


def test_source_extraction_deduplicates_urls():
    payload = {
        "output": [
            {"action": {"sources": [
                {"title": "Official", "url": "https://example.com/p"},
                {"title": "Official duplicate", "url": "https://example.com/p"},
                {"title": "Retail", "url": "https://shop.example.com/p"},
            ]}}
        ]
    }
    assert _extract_sources(payload) == [
        {"title": "Official", "url": "https://example.com/p"},
        {"title": "Retail", "url": "https://shop.example.com/p"},
    ]


def test_prose_search_result_recovers_one_product():
    item = _fallback_product_from_text(
        "商品名稱：**多容安舒緩保濕面霜 40ml**\n官方來源：品牌官網",
        brand_hint="理膚寶水",
        category="乳液",
    )
    assert item["name"] == "多容安舒緩保濕面霜 40ml"
    assert item["brand"] == "理膚寶水"
    assert item["price"] is None


def test_official_page_title_is_reduced_to_product_name():
    title = (
        "敏感肌保濕化妝水推薦：多容安舒緩保濕化妝水 200ml(安心露) "
        "- 理膚寶水"
    )
    assert _clean_product_title(title, "理膚寶水") == (
        "多容安舒緩保濕化妝水 200ml(安心露)"
    )


def test_retail_title_strips_site_suffix_and_brand_wrapper():
    title = '"【理膚寶水】 B5全面修復霜 | @cosme"'
    assert _clean_product_title(title, "理膚寶水") == "B5全面修復霜"


class _FakeResponse:
    output_text = """{"products":[
        {"name":"A 保濕霜","brand":"測試牌","category":"乳液","price":10,"currency":"USD","summary":"依官方資訊，這款產品著重日常保濕，適合想加強乾燥照護的需求。","source_url":"https://brand.example/a"},
        {"name":"B 精華","brand":"測試牌","category":"精華液","price":null,"currency":"TWD","summary":"官方頁面說明其清爽質地，可作為偏好輕盈保養時的參考。","source_url":"https://brand.example/b"},
        {"name":"C 化妝水","brand":"測試牌","category":"化妝水","price":null,"currency":"TWD","summary":"官方商品資訊以補水保養為主，符合希望簡化日常步驟的需求。","source_url":"https://brand.example/c"}
    ]}"""

    def model_dump(self):
        return {"output": [{"action": {"sources": [
            {"title": "A", "url": "https://brand.example/a"},
            {"title": "B", "url": "https://brand.example/b"},
            {"title": "C", "url": "https://brand.example/c"},
        ]}}]}


class _FakeResponses:
    @staticmethod
    def create(**_kwargs):
        return _FakeResponse()


class _FakeClient:
    responses = _FakeResponses()


def test_external_search_returns_three_products_with_one_reference_each(monkeypatch):
    monkeypatch.setenv("PRODUCT_USD_TWD_RATE", "32")
    result = search_named_product(
        "測試牌保濕產品",
        concerns=["保濕"],
        brand_hint="測試牌",
        max_results=3,
        client=_FakeClient(),
    )

    assert len(result["products"]) == 3
    assert result["products"][0]["price"] == 320
    assert all(product["currency"] == "TWD" for product in result["products"])
    assert all(len(product["sources"]) == 1 for product in result["products"])
    assert all("|" not in product["copy"] for product in result["products"])


def test_generic_trend_query_stays_out_of_named_product_web_search():
    intent = SimpleNamespace(
        raw_query="最新流行的保濕產品",
        brand_preference=[],
    )
    assert is_trending_product_query(intent) is True
    assert should_search_named_product(intent) is False



def test_tavily_filters_articles_and_returns_a_cited_product(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "title": "2026 Best 理膚寶水精華推薦",
                        "content": "理膚寶水熱門商品排行文章",
                        "url": "https://example.com/list",
                    },
                    {
                        "title": "修復霜",
                        "content": "理膚寶水修復霜系列",
                        "url": "https://www.lrp.com.tw/Product/List/Index/TREATMENT?MenuType=ProductType",
                    },
                    {
                        "title": "理膚寶水 B5 全面修復霜活動專區",
                        "content": "理膚寶水活動頁",
                        "url": "https://www.lrp.com.tw/events/cica2021q4",
                    },
                    {
                        "title": "理膚寶水B5原來有4種，究竟該怎麼挑？",
                        "content": "理膚寶水 B5 美妝媒體文章",
                        "url": "https://www.marieclaire.com.tw/beauty/skin-care/69212",
                    },
                    {
                        "title": "理膚寶水 B5 修復精華",
                        "content": "理膚寶水官方商品資料與使用方式",
                        "url": "https://example.com/product",
                    },
                    {
                        "title": "其他品牌保濕精華",
                        "content": "不相關的商品頁",
                        "url": "https://example.com/other",
                    },
                ]
            }

    monkeypatch.setattr(
        "tools.search.product_web_search.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )
    result = _search_named_product_tavily(
        "理膚寶水 B5 修復精華好用嗎",
        skin_type="sensitive",
        concerns=["修護"],
        category="精華液",
        brand_hint="理膚寶水",
        max_results=3,
    )
    assert len(result["products"]) == 1
    product = result["products"][0]
    assert product["is_web_result"] is True
    assert product["source_url"] == "https://example.com/product"
    assert product["sources"] == result["sources"]


def test_product_evidence_packet_is_the_contract_terra_consumes():
    packet = build_product_evidence_packet(
        query="AKIMIA 藍銅胜肽微電流面膜適合什麼膚質？",
        products=[
            {
                "name": "AKIMIA 藍銅胜肽微電流面膜",
                "brand": "AKIMIA",
                "category": "面膜",
                "concerns": ["乾燥"],
                "sources": [
                    {
                        "source_type": "official",
                        "title": "官方商品頁",
                        "url": "https://www.akimia.com/product",
                    }
                ],
            }
        ],
        provider="tavily",
        requested_fields=["skin_types"],
        fallback_reason="local_catalogue_miss",
        request_id="req-123",
        duration_ms=321,
        cache_hit=False,
    )

    assert packet["schema_version"] == "1.0"
    assert packet["provider"] == "tavily"
    assert packet["requested_fields"] == ["skin_types"]
    assert packet["request"]["provider_request_id"] == "req-123"
    assert packet["request"]["duration_ms"] == 321
    assert packet["request"]["cache_hit"] is False
    assert packet["items"][0]["name"] == "AKIMIA 藍銅胜肽微電流面膜"
    assert packet["items"][0]["sources"][0]["source_type"] == "official"


def test_extract_explicit_skin_types_requires_suitability_language():
    assert _extract_explicit_skin_types("含有適合敏感肌與乾性肌膚使用的配方") == [
        "敏感肌",
        "乾性肌膚",
    ]
    assert _extract_explicit_skin_types("成分介紹提到敏感肌，但沒有適用說明") == []


def test_extract_explicit_skin_types_handles_all_skin_types():
    assert _extract_explicit_skin_types("本產品適用所有膚質") == ["所有膚質"]
