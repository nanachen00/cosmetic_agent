from scripts.backfill_scraped_product_catalog import (
    clean_highlight_ingredients,
    cleanup_existing,
    infer_category,
    is_skincare_candidate,
)


def test_highlight_ingredients_exclude_scent_descriptors():
    assert clean_highlight_ingredients(
        [
            "神經醯胺NP",
            "茉莉花香",
            "白桃香",
            "柑橘木質香調",
            "薰衣草香氛",
            "保濕",
            "美白",
            "滋潤",
            "水",
            "茶樹精油",
        ]
    ) == ["神經醯胺NP", "茶樹精油"]


def test_non_skincare_sprays_and_eye_masks_are_not_misclassified():
    assert infer_category("3CE 定妝噴霧") == ""
    assert infer_category("41度C蒸氣熱敷眼罩") == ""
    assert infer_category("保濕噴霧") == "化妝水"


def test_skincare_only_filter_prioritizes_face_care():
    assert is_skincare_candidate({"name": "玻尿酸保濕精華", "category": "臉部保養"})
    assert is_skincare_candidate({"name": "溫和眼唇卸妝液", "category": ""})
    assert is_skincare_candidate({"name": "SPF50 水感防曬乳", "category": ""})
    assert is_skincare_candidate({"name": "SPF50 防曬噴霧（定妝控油）", "category": "防曬乳"})
    assert is_skincare_candidate({"name": "SPF30 修護護唇膏", "category": "護唇"})
    assert not is_skincare_candidate({"name": "持久定妝保濕噴霧", "category": "化妝水"})
    assert not is_skincare_candidate({"name": "蒸氣熱敷眼罩", "category": "面膜"})
    assert not is_skincare_candidate({"name": "修護洗髮精華", "category": "精華液"})
    assert not is_skincare_candidate({"name": "臉部緊緻刮痧板組", "category": "臉部保養"})
    assert not is_skincare_candidate({"name": "拋棄式洗臉巾80入", "category": "洗面乳"})


def test_product_name_form_takes_priority_over_bad_external_category():
    product_name = "AQUALABEL 全能5D緊緻精華乳（極潤）"
    assert infer_category(product_name) == "精華液"
    assert (infer_category(product_name) or infer_category("面膜")) == "精華液"
    assert infer_category("SPF30 修護護唇膏") == "護唇"


class _CleanupSession:
    def __init__(self, row=None):
        self.writes = []
        self.row = row or {
            "id": "one",
            "name": "全能5D緊緻精華乳",
            "category": "面膜",
            "category_evidence": "official_product_page",
            "key_ingredients": ["美白", "神經醯胺NP"],
        }

    def run(self, query, **params):
        if "RETURN elementId(p) AS id" in query:
            return [self.row]
        self.writes.append((query, params))

        class _Result:
            def consume(self):
                return None

        return _Result()


def test_cleanup_existing_revalidates_pipeline_fields():
    session = _CleanupSession()
    stats = cleanup_existing(session, "cosmed", apply=True)
    assert stats == {"ingredient_rows_cleaned": 1, "categories_corrected": 1}
    assert any(write[1].get("items") == ["神經醯胺NP"] for write in session.writes)
    assert any(write[1].get("category") == "精華液" for write in session.writes)


def test_cleanup_keeps_supported_category_when_name_has_no_form_hint():
    session = _CleanupSession(
        {
            "id": "two",
            "name": "品牌水光系列商品",
            "category": "臉部保養",
            "category_evidence": "official_product_page",
            "key_ingredients": [],
        }
    )
    stats = cleanup_existing(session, "cosmed", apply=True)
    assert stats == {"ingredient_rows_cleaned": 0, "categories_corrected": 0}
    assert session.writes == []
