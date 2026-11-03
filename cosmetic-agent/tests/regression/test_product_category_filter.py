"""Web 商品結果必須遵守使用者指定品類（硬性條件）。"""
from tools.search.product_web_search import _matches_requested_category


def test_mask_rejected_when_lotion_requested():
    item = {"name": "B5全面修復保濕面膜 升級版_5片入", "category": "面膜"}
    assert not _matches_requested_category(item, "乳液")


def test_cream_accepted_when_lotion_requested():
    item = {"name": "多容安舒緩保濕乳霜", "category": ""}
    assert _matches_requested_category(item, "乳液")


def test_lotion_accepted_when_cream_requested():
    item = {"name": "理必佳清爽保濕乳液", "category": ""}
    assert _matches_requested_category(item, "乳霜")


def test_unknown_or_missing_category_passes():
    item = {"name": "任意商品", "category": ""}
    assert _matches_requested_category(item, None)
    assert _matches_requested_category(item, "不明")
    assert _matches_requested_category(item, "未指定")


def test_stated_category_counts_even_if_name_silent():
    item = {"name": "神秘保養品", "category": "乳霜"}
    assert _matches_requested_category(item, "乳液")
