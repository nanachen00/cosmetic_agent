import json

import pytest

import app.services.product_review_service as service


def _seed(tmp_path):
    seed = {
        "sources": {"source-a": {"name": "來源 A", "url": "https://example.com/a"}},
        "products": [
            {
                "canonical_key": "one",
                "brand": "Brand",
                "name_zh": "保濕修護精華",
                "category": "精華液",
                "cohort": "classic",
                "concerns": ["保濕", "修護"],
                "verified_functional_categories": ["hydration_repair"],
                "evidence": [{"source_id": "source-a", "rank": 1}],
            },
            {
                "canonical_key": "two",
                "brand": "Brand",
                "name_zh": "清爽臉部防曬",
                "category": "臉部防曬",
                "cohort": "trending",
                "concerns": ["防曬"],
                "verified_functional_categories": ["sun_protection"],
                "evidence": [],
            },
        ],
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    manifest = tmp_path / "sources.json"
    brand_registry = tmp_path / "brand_market_registry.json"
    name_registry = tmp_path / "product_name_registry.json"
    decisions = tmp_path / "decisions.json"
    first.write_text(json.dumps(seed), encoding="utf-8")
    second.write_text(json.dumps({"products": []}), encoding="utf-8")
    manifest.write_text(json.dumps({"sources": []}), encoding="utf-8")
    brand_registry.write_text(
        json.dumps(
            {
                "markets": {"japan": "日系品牌", "unverified": "來源待核對"},
                "brands": {
                    "Brand": {
                        "market": "japan",
                        "country_zh": "日本",
                        "confidence": "high",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    name_registry.write_text(
        json.dumps(
            {
                "products": {
                    "one": {
                        "official_name": "Hydrating Repair Serum",
                        "taiwan_official_name": "保濕修護精華",
                        "common_names": ["小水滴"],
                        "ingredients_inci": ["Aqua", "Glycerin", "Butylene Glycol", "Panthenol", "Ceramide NP"],
                        "inci_source_url": "https://brand.example.tw/products/one",
                        "formula_market": "tw",
                        "verification_status": "verified",
                        "confidence": "high",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service._DATA_DIR = tmp_path
    service._SEED_FILES = (first, second)
    service._SOURCE_MANIFEST = manifest
    service._BRAND_MARKET_REGISTRY = brand_registry
    service._PRODUCT_NAME_REGISTRY = name_registry
    service._DECISIONS_FILE = decisions
    return decisions


def test_groups_and_filters_without_touching_neo4j(tmp_path):
    _seed(tmp_path)
    stats = service.product_review_stats()
    assert stats["total"] == 2
    assert next(row for row in stats["functional_categories"] if row["key"] == "hydration_repair")["count"] == 1
    assert service.list_product_reviews(functional_category="sun_protection")["items"][0]["canonical_key"] == "two"
    assert next(row for row in stats["brand_markets"] if row["key"] == "japan")["count"] == 2
    assert stats["brands"] == [{"brand": "Brand", "count": 2}]
    assert stats["import_status_counts"] == {"not_requested": 2}
    named = service.list_product_reviews(search="小水滴")["items"][0]
    assert named["official_name"] == "Hydrating Repair Serum"
    assert named["ingredients_inci"][:2] == ["Aqua", "Glycerin"]
    assert named["inci_status"] == "full"
    assert named["quality_blockers"] == []


def test_approval_imports_once_and_is_idempotent(tmp_path, monkeypatch):
    decisions = _seed(tmp_path)
    calls = []

    def fake_import(product, *, reviewer_id_hash, imported_at):
        calls.append(product["canonical_key"])
        return {
            "import_status": "imported",
            "imported_at": imported_at,
            "import_error": "",
            "neo4j_sku": "BLINK-REVIEW-TEST",
            "neo4j_element_id": "node-1",
            "recommendable": True,
        }

    monkeypatch.setattr(service, "_import_product_to_neo4j", fake_import)
    result = service.update_product_review(
        "one", review_status="approved", reviewer_notes="已核對", reviewer_id_hash="admin"
    )
    assert result["review_status"] == "approved"
    assert result["import_status"] == "imported"
    assert result["recommendable"] is True
    assert json.loads(decisions.read_text(encoding="utf-8"))["one"]["neo4j_sku"] == "BLINK-REVIEW-TEST"

    repeated = service.update_product_review("one", review_status="approved")
    assert repeated["neo4j_element_id"] == "node-1"
    assert calls == ["one"]


def test_failed_import_is_visible_and_retryable(tmp_path, monkeypatch):
    decisions = _seed(tmp_path)
    attempts = {"count": 0}

    def flaky_import(product, *, reviewer_id_hash, imported_at):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("embedding service unavailable")
        return {
            "import_status": "imported",
            "imported_at": imported_at,
            "import_error": "",
            "neo4j_sku": "BLINK-REVIEW-RETRY",
            "neo4j_element_id": "node-2",
            "recommendable": True,
        }

    monkeypatch.setattr(service, "_import_product_to_neo4j", flaky_import)
    with pytest.raises(service.ProductImportError):
        service.update_product_review("one", review_status="approved")
    failed = json.loads(decisions.read_text(encoding="utf-8"))["one"]
    assert failed["import_status"] == "failed"
    assert failed["recommendable"] is False
    assert "embedding service unavailable" in failed["import_error"]

    retried = service.update_product_review("one", review_status="approved")
    assert retried["import_status"] == "imported"
    assert attempts["count"] == 2

def test_import_rejects_unverified_english_name_before_embedding():
    product = {
        "canonical_key": "english-only",
        "name_zh": "Hydrating Face Serum",
        "official_name": "Hydrating Face Serum",
        "taiwan_official_name": "",
        "name_verification_status": "needs_official_chinese_name",
        "review_blockers": ["missing_official_chinese_name"],
    }
    with pytest.raises(ValueError, match="商品尚未通過品質檢查"):
        service._import_product_to_neo4j(
            product,
            reviewer_id_hash="reviewer",
            imported_at="2026-09-27T00:00:00+00:00",
        )




def test_unverified_ingredient_tags_do_not_pass_quality_gate():
    product = {
        "canonical_key": "tag-only",
        "brand": "AHC",
        "canonical_brand": "AHC",
        "taiwan_official_name": "AHC 保濕精華",
        "name_verification_status": "verified",
        "brand_origin_confidence": "high",
        "ingredient_tags": ["玻尿酸", "泛醇 B5"],
        "ingredients_inci": [],
        "inci_status": "missing",
        "formula_market": "tw",
    }
    blockers = service._catalog_quality_blockers(product)
    assert "missing_verified_featured_ingredients" in blockers
    assert product["ingredient_tags"] != product["ingredients_inci"]


def test_official_featured_ingredients_can_pass_without_full_inci():
    product = {
        "canonical_key": "official-tags",
        "brand": "AHC",
        "canonical_brand": "AHC",
        "taiwan_official_name": "AHC 保濕精華",
        "name_verification_status": "verified",
        "brand_origin_confidence": "high",
        "ingredient_tags": ["玻尿酸", "泛醇 B5"],
        "ingredients_inci": [],
        "inci_status": "missing",
        "claim_evidence": [{
            "source_url": "https://brand.example.tw/products/official-tags",
            "context": "官方主打玻尿酸與泛醇",
            "evidence_type": "official_claim",
        }],
    }
    assert service._catalog_quality_blockers(product) == []


def test_product_name_and_concerns_never_invent_ingredient_tags():
    product = {
        "name_zh": "A醇水楊酸美白精華",
        "concerns": ["玻尿酸保濕"],
        "evidence": [{"context": "熱門排行"}],
    }
    assert service._ingredient_tags(product) == []


def test_verified_retinoid_tag_adds_pregnancy_warning():
    notes = service._ingredient_safety_notes({
        "ingredient_tags": ["HYDROXYPINACOLONE RETINOATE"],
    })
    assert any(note["rule_id"] == "retinoid_pregnancy" for note in notes)


def test_capacity_only_variants_share_family_key():
    base = {"canonical_brand": "NIVEA", "taiwan_official_name": "妮維雅 LUMINOUS630 淡斑煥白精華30ml"}
    large = {"canonical_brand": "NIVEA", "taiwan_official_name": "妮維雅 LUMINOUS630 淡斑煥白精華50ml"}
    assert service._canonical_family_key(base) == service._canonical_family_key(large)


def test_concentration_and_spf_remain_distinct_products():
    one = {"canonical_brand": "PAULA'S CHOICE", "taiwan_official_name": "1% 水楊酸精華 30ml"}
    two = {"canonical_brand": "PAULA'S CHOICE", "taiwan_official_name": "2% 水楊酸精華 30ml"}
    spf30 = {"canonical_brand": "Brand", "taiwan_official_name": "臉部防曬 SPF30 50ml"}
    spf50 = {"canonical_brand": "Brand", "taiwan_official_name": "臉部防曬 SPF50 50ml"}
    assert service._canonical_family_key(one) != service._canonical_family_key(two)
    assert service._canonical_family_key(spf30) != service._canonical_family_key(spf50)


def test_popularity_or_name_text_does_not_create_verified_efficacy():
    product = {
        "name_zh": "NEW Best Seller CICA 美白抗痘精華",
        "concerns": ["保濕", "美白", "痘痘"],
        "evidence": [{"context": "Best seller", "source_url": "https://shop.example/ranking"}],
    }
    assert service._functional_categories(product) == ["general_care"]
    assert set(service._suggested_functional_categories(product)) >= {"brightening", "acne_pores"}


def test_product_label_safety_note_accepts_note_and_evidence_url():
    notes = service._ingredient_safety_notes(
        {
            "ingredient_tags": [],
            "contraindication_notes": [
                {
                    "note": "官方要求避開傷口，若出現紅腫應停止使用。",
                    "evidence_url": "https://brand.example/products/one",
                    "population": "pregnant_or_breastfeeding",
                }
            ],
        }
    )
    assert notes[0]["message"] == "官方要求避開傷口，若出現紅腫應停止使用。"
    assert notes[0]["source_url"] == "https://brand.example/products/one"
    assert notes[0]["severity"] == "product_label_caution"
    assert notes[0]["audiences"] == ["懷孕／哺乳族群", "傷口／醫美術後肌膚"]


def test_product_label_safety_note_string_uses_verified_claim_source():
    notes = service._ingredient_safety_notes(
        {
            "ingredient_tags": [],
            "contraindication_notes": ["敏感肌先在前臂少量測試。"],
            "claim_evidence": [
                {
                    "source_url": "https://brand.example/products/two",
                    "evidence_type": "official_claim",
                }
            ],
        }
    )
    assert notes[0]["message"] == "敏感肌先在前臂少量測試。"
    assert notes[0]["source_url"] == "https://brand.example/products/two"
    assert notes[0]["audiences"] == ["敏感肌／過敏族群"]

