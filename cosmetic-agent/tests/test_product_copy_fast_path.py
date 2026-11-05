from agents.product import product_agent
import requests


def test_product_copy_uses_evidence_fast_path_by_default(monkeypatch):
    monkeypatch.delenv("PRODUCT_COPY_USE_LLM", raising=False)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("product copy fast path must not call TAIDE")

    monkeypatch.setattr(requests, "post", unexpected_request)
    products = [
        {
            "name": "測試保濕精華",
            "ingredients": ["玻尿酸", "神經醯胺"],
            "concern_hits": 2,
            "matched_claim_terms": ["無香料"],
            "price": 880,
            "over_budget": False,
            "is_partner": False,
        }
    ]

    result = product_agent._batch_generate_rank_content(
        products,
        skin_type="sensitive",
        concerns=["保濕", "舒緩"],
        warnings=[],
        memory_context="",
    )

    assert result is products
    assert "玻尿酸" in result[0]["copy"]
    assert "商品資料列出的成分包含" in result[0]["copy"]
    assert "配方資料已可核對" not in result[0]["copy"]
    assert "需求有直接匹配" in result[0]["recommendation_reason"]


def test_product_copy_explains_incomplete_ingredients_in_plain_language(monkeypatch):
    monkeypatch.delenv("PRODUCT_COPY_USE_LLM", raising=False)
    products = [
        {
            "name": "測試乳霜",
            "ingredients": [],
            "concern_hits": 1,
            "matched_claim_terms": ["舒敏"],
            "price": 299,
            "over_budget": False,
            "is_partner": True,
        }
    ]

    result = product_agent._batch_generate_rank_content(
        products,
        skin_type="dry",
        concerns=["舒敏"],
        warnings=[],
        memory_context="",
    )

    assert result[0]["copy"] == (
        "此商品符合目前的保養商品與舒敏篩選條件；"
        "可從商品頁核對容量、用法與適用部位。"
    )


def test_recommendation_reason_is_chinese_first_and_specific():
    reason = product_agent._ingredient_support_reason("vitamin c", "美白")
    assert reason == "維他命 C提供抗氧化與提亮支持"


def test_incomplete_legacy_product_is_rejected():
    product = {"name": "杜鵑花酸激光淨白超導液30ml", "brand": "CICIBELLA", "category": "精華液", "price": 890}
    assert not product_agent._is_display_ready_product(product)


def test_marketing_copy_is_not_ingredient_evidence():
    product = {
        "name": "Curel類神經醯胺極護水精華",
        "brand": "Curel",
        "ingredients_list": ["類神經醯胺極護水精華有效緩解因乾燥引起的刺癢、反覆泛紅"],
        "source_url": "https://example.com/product",
        "source_type": "migrated",
    }
    assert not product_agent._is_display_ready_product(product)


def test_discrete_chinese_ingredient_names_are_verified_evidence():
    product = {
        "name": "水亮撫紋精華液",
        "brand": "FORMULIER",
        "key_ingredients": ["神經醯胺NP", "小分子玻尿酸"],
        "source_url": "https://shop.cosmed.com.tw/SalePage/Index/11139863",
        "source_type": "scraped",
    }
    assert product_agent._is_display_ready_product(product)


def test_official_highlights_require_traceable_ingredient_source():
    product = {
        "name": "官方重點成分精華",
        "brand": "測試品牌",
        "key_ingredients": ["亞麻籽萃取物48%"],
        "ingredient_evidence_level": "official_highlights",
        "source_url": "https://example.com/product",
        "source_type": "scraped",
    }
    assert not product_agent._is_display_ready_product(product)

    product["ingredient_source_url"] = product["source_url"]
    assert product_agent._is_display_ready_product(product)


def test_reviewed_description_can_supply_content_evidence():
    product = {
        "name": "已審核精華",
        "brand": "測試品牌",
        "description": "已人工核對商品用途、使用方式及完整來源，可供推薦前進一步比較。",
        "source_url": "https://example.com/product",
        "source_type": "admin_approved",
    }
    assert product_agent._is_display_ready_product(product)


def test_display_ingredients_are_deduped_after_localization():
    values = ["niacinamide", "菸鹼醯胺（Niacinamide）", "niacinamide"]
    assert [value.strip() for value in product_agent._dedupe_display_ingredients(values)] == [
        "菸鹼醯胺"
    ]


def test_ingredient_copy_uses_chinese_names_without_changing_product_or_link():
    from agents.root.orchestrator import _format_ranked_products
    from schemas.intent import IntentParseResult, IntentType

    product = {
        "id": "demo",
        "name": "Niacinamide 亮白乳液",
        "brand": "Example Brand",
        "category": "乳液",
        "price": 229,
        "rank_reason": "菸鹼醯胺（Niacinamide）可協助調理油脂",
        "key_ingredients": [
            "菸鹼醯胺（Niacinamide）",
            "傳明酸（Tranexamic Acid）",
        ],
        "copy": "添加 Tranexamic Acid，協助改善暗沉外觀。",
        "sources": [{"url": "https://example.com/Niacinamide"}],
    }
    reply = _format_ranked_products(
        {
            "products": [product],
            "terra_intro": "可留意菸鹼醯胺（Niacinamide）與傳明酸（Tranexamic Acid）。",
        },
        {},
        IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query="找美白乳液"),
    )
    assert "菸鹼醯胺可協助調理油脂" in reply
    assert "重點成分：** 菸鹼醯胺、傳明酸" in reply
    assert "Niacinamide 亮白乳液" in reply
    assert "https://example.com/Niacinamide" in reply
    assert "（Niacinamide）" not in reply
    assert "商品特色" not in reply


def test_mobile_product_reply_uses_profile_history_and_only_three_items():
    from agents.root.orchestrator import _format_ranked_products
    from schemas.intent import IntentParseResult, IntentType, ProductCategory

    products = [
        {
            "id": f"demo-{index}",
            "name": f"保濕乳液 {index}",
            "brand": "Demo",
            "category": "乳液",
            "price": 300 + index,
            "recommendation_reason": "符合清爽保濕與日間使用需求",
            "ingredients": ["玻尿酸", "神經醯胺"],
        }
        for index in range(1, 5)
    ]
    reply = _format_ranked_products(
        {
            "products": products,
            "personalization_context": {
                "applied": [
                    "膚質：混合肌",
                    "避開：香料、酒精",
                    "近期膚況：保濕、抗痘",
                    "質地偏好：清爽",
                    "預算：1000元以內",
                ]
            },
        },
        {},
        IntentParseResult(
            intent_type=IntentType.FIND_PRODUCT,
            product_category=ProductCategory.LOTION,
            raw_query="幫我找一瓶千元內白天可用的保濕乳液",
        ),
    )

    assert "已自動套用你的資料" in reply
    assert "近期膚況：保濕、抗痘" in reply
    assert "### 3." in reply
    assert "保濕乳液 4" not in reply
    assert "商品特色" not in reply


def test_allergen_filter_covers_catalog_graph_extracted_and_unknown_ingredients():
    candidates = [
        {"id": "catalog-hit", "ingredients_list": ["Aqua", "Parfum"]},
        {"id": "graph-hit", "ingredients_list": ["Aqua"]},
        {"id": "extract-hit", "extracted_ingredients": ["香料"]},
        {"id": "safe", "key_ingredients": ["玻尿酸", "神經醯胺"]},
        {"id": "unknown"},
    ]
    graph_rows = [
        {
            "product_id": "graph-hit",
            "ingredient_name_zh": "香料",
            "ingredient_name_en": "Fragrance",
        }
    ]

    filtered = product_agent._filter_disliked_ingredients(
        candidates,
        ["香料"],
        graph_rows,
    )

    assert [product["id"] for product in filtered] == ["safe"]


def test_fragrance_allergy_filters_common_declared_fragrance_sensitizers():
    candidates = [
        {"id": "lavender", "key_ingredients": ["Lavandula Angustifolia Extract"]},
        {"id": "linalool", "ingredients_list": ["Aqua", "Linalool"]},
        {"id": "safe", "key_ingredients": ["玻尿酸", "神經醯胺"]},
    ]

    filtered = product_agent._filter_disliked_ingredients(
        candidates,
        ["香料"],
        [],
    )

    assert [product["id"] for product in filtered] == ["safe"]


def test_graph_warning_penalizes_only_the_product_with_the_warned_ingredient():
    rows = [
        {
            "product_id": "risky",
            "ingredient_name_zh": "水楊酸",
            "ingredient_name_en": "Salicylic Acid",
        },
        {
            "product_id": "safe",
            "ingredient_name_zh": "玻尿酸",
            "ingredient_name_en": "Hyaluronic Acid",
        },
    ]

    assert product_agent._warned_product_ids(["水楊酸不建議敏感肌使用"], rows) == {
        "risky"
    }


def test_no_product_message_dict_is_rendered_as_plain_mobile_copy():
    from agents.root.orchestrator import _format_ranked_products
    from schemas.intent import IntentParseResult, IntentType

    reply = _format_ranked_products(
        {
            "products": [],
            "message": {
                "main": "目前沒有可安全核對的商品。",
                "sub": "可改品類再查。",
            },
        },
        {},
        IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query="找乳液"),
    )

    assert reply == "目前沒有可安全核對的商品。\n- 可改品類再查。"


def test_chinese_ingredient_cleaner_keeps_unrelated_english_parentheses():
    assert product_agent.clean_ingredient_display("適合油性肌（Oily Skin）") == "適合油性肌（Oily Skin）"


def test_ingredient_cleaner_preserves_original_product_name():
    name = "菸鹼醯胺（Niacinamide）亮白乳液"
    text = f"### {name}\n菸鹼醯胺（Niacinamide）可協助調理油脂。"
    assert product_agent.clean_ingredient_display(text, [name]) == (
        f"### {name}\n菸鹼醯胺可協助調理油脂。"
    )


def test_product_response_composer_cannot_reintroduce_english_ingredients():
    from types import SimpleNamespace

    from agents.root.orchestrator import RootAgentOrchestrator
    from schemas.intent import IntentParseResult, IntentType

    agent = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    agent.state = SimpleNamespace(
        last_products=[{"name": "Niacinamide 亮白乳液", "brand": "Example Brand"}],
        history=[],
    )
    agent.response_composer = SimpleNamespace(
        compose=lambda **kwargs: SimpleNamespace(
            reply="Niacinamide 亮白乳液含菸鹼醯胺（Niacinamide）。",
            used_model=True,
        )
    )
    agent._log = lambda *args: None
    result = agent._compose_agent_reply(
        IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query="找亮白乳液"),
        "Niacinamide 亮白乳液含菸鹼醯胺。",
    )
    assert result == "Niacinamide 亮白乳液含菸鹼醯胺。"


def test_basic_catalog_product_remains_usable():
    product = {
        "id": "basic-1",
        "name": "亮白精華液 30ml",
        "brand": "示例品牌",
        "category": "精華液",
        "price": 499,
        "source_url": "https://example.com/basic-1",
    }
    assert product_agent._product_data_quality(product) == "basic"
    assert product_agent._is_usable_product(product)
    assert not product_agent._is_display_ready_product(product)


def test_catalog_product_without_source_link_stays_blocked():
    product = {
        "id": "blocked-1",
        "name": "保濕精華液",
        "brand": "示例品牌",
        "category": "精華液",
        "price": 399,
    }
    assert product_agent._product_data_quality(product) == "blocked"
    assert not product_agent._is_usable_product(product)


def test_verified_product_ranks_before_basic_product():
    verified = {
        "id": "verified-1",
        "name": "完整精華液",
        "brand": "示例品牌",
        "price": 500,
        "source_url": "https://example.com/verified-1",
        "ingredients_list": ["niacinamide"],
    }
    basic = {
        "id": "basic-1",
        "name": "基本精華液",
        "brand": "示例品牌",
        "price": 500,
        "source_url": "https://example.com/basic-1",
    }
    verified_key = product_agent._product_rank_key(verified, set(), None)
    basic_key = product_agent._product_rank_key(basic, set(), None)
    assert verified_key < basic_key
