#!/usr/bin/env python3
"""套用商品品類修正（乳液/乳霜 bug + web 結果品類硬過濾 + 文案清洗）。

用法：在 /var/lib/jenkins/tomod/cosmetic-agent 目錄下執行
    python3 apply_category_fix.py

每個檔案修改前會建立 .bak_20260920_catfix 備份。
任何錨點找不到或出現多次時，直接中止且不寫入該檔。
"""
import py_compile
import shutil
import sys

REPO = "."
BAK_SUFFIX = ".bak_20260920_catfix"


def edit(path: str, replacements: list[tuple[str, str]]) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    original = text
    for anchor, new in replacements:
        count = text.count(anchor)
        if count != 1:
            raise SystemExit(
                f"[中止] {path}: 錨點出現 {count} 次（需恰好 1 次）：\n---\n{anchor}\n---"
            )
        text = text.replace(anchor, new)
    shutil.copyfile(path, path + BAK_SUFFIX)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"[OK] 已修改 {path}（備份 {path + BAK_SUFFIX}）")
    _ = original  # keep for clarity


# ── A. agents/product/product_agent.py ──────────────────────────────
A_PATH = f"{REPO}/agents/product/product_agent.py"
A_EDITS = [
    # A1: CATEGORY_MAP 支援乳霜（ProductCategory.CREAM）
    (
        '    "乳液":    ["乳液"],\n',
        '    "乳液":    ["乳液", "乳霜"],\n    "乳霜":    ["乳霜", "乳液"],\n',
    ),
    # A2: 名稱關鍵字也補乳霜
    (
        '    "乳液": ["乳液", "乳霜", "凝乳", "保濕乳", "修護乳"],\n',
        '    "乳液": ["乳液", "乳霜", "凝乳", "保濕乳", "修護乳"],\n'
        '    "乳霜": ["乳霜", "面霜", "乳液", "凝霜", "保濕霜", "修護霜"],\n',
    ),
    # A3: TAIDE 批次文案也做去 emoji / 去直線分隔符清洗
    (
        "                p['copy'] = copy_text[:120]\n",
        "                copy_text = re.sub(r\"[|｜]+\", \"，\", copy_text)\n"
        "                copy_text = re.sub(\n"
        "                    \"[\\U0001F300-\\U0001FAFF\\U00002600-\\U000027BF]\",\n"
        "                    \"\",\n"
        "                    copy_text,\n"
        "                )\n"
        "                p['copy'] = copy_text[:120]\n",
    ),
]

# ── B. tools/search/product_web_search.py ───────────────────────────
B_PATH = f"{REPO}/tools/search/product_web_search.py"

B_MATCHER = '''    return fallback or "其他"


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
'''

B_EDITS = [
    # B1: 加入品類關鍵字表與 hard filter 函式（插在 _infer_category 之後）
    (
        '    return fallback or "其他"\n',
        B_MATCHER,
    ),
    # B2: 品類寫進搜尋語句 + prompt 明示品類為硬性條件
    (
        '    category_text = category if category and category != "不明" else "未指定"\n',
        '    category_text = category if category and category != "不明" else "未指定"\n'
        '    if category_text != "未指定" and category_text not in query:\n'
        '        query = f"{query} {category_text}"\n'
        '    category_rule = (\n'
        '        "品類是硬性條件：每一筆都必須是此品類的商品，其他品類一律不得列入。"\n'
        '        if category_text != "未指定"\n'
        '        else ""\n'
        '    )\n',
    ),
    (
        '        f"膚質 {skin_type}；需求 {concern_text}；品類 {category_text}。"\n',
        '        f"膚質 {skin_type}；需求 {concern_text}；品類 {category_text}。{category_rule}"\n',
    ),
    # B3: 模型首輪回傳先過品類
    (
        "        existing = [\n"
        "            item for item in (parsed.get(\"products\") or [])\n"
        "            if isinstance(item, dict)\n"
        "        ]\n",
        "        existing = [\n"
        "            item for item in (parsed.get(\"products\") or [])\n"
        "            if isinstance(item, dict)\n"
        "            and _matches_requested_category(item, category)\n"
        "        ]\n",
    ),
    # B4: 來源頁 fallback 也過品類
    (
        "            if source_product:\n"
        "                existing.append(source_product)\n",
        "            if source_product and _matches_requested_category(\n"
        "                source_product, category\n"
        "            ):\n"
        "                existing.append(source_product)\n",
    ),
    # B5: followup 補齊時也過品類
    (
        "            if not source_product:\n"
        "                continue\n"
        "            candidate_name = re.sub(\n",
        "            if not source_product:\n"
        "                continue\n"
        "            if not _matches_requested_category(source_product, category):\n"
        "                continue\n"
        "            candidate_name = re.sub(\n",
    ),
    # B6: 最終組裝迴圈的保險
    (
        "            if not name or not summary or not sources:\n"
        "                continue\n",
        "            if not name or not summary or not sources:\n"
        "                continue\n"
        "            if not _matches_requested_category(item, category):\n"
        "                continue\n",
    ),
]

# ── C. agents/root/orchestrator.py ──────────────────────────────────
C_PATH = f"{REPO}/agents/root/orchestrator.py"
C_EDITS = [
    (
        '                "精華": "精華液", "精華液": "精華液", "乳液": "乳液",\n',
        '                "精華": "精華液", "精華液": "精華液", "乳液": "乳液",\n'
        '                "乳液/乳霜": "乳液",\n',
    ),
]

# ── D. 新增回歸測試 ──────────────────────────────────────────────────
D_PATH = f"{REPO}/tests/regression/test_product_category_filter.py"
D_CONTENT = '''"""Web 商品結果必須遵守使用者指定品類（硬性條件）。"""
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
'''


def main() -> None:
    edit(A_PATH, A_EDITS)
    edit(B_PATH, B_EDITS)
    edit(C_PATH, C_EDITS)
    with open(D_PATH, "w", encoding="utf-8") as fh:
        fh.write(D_CONTENT)
    print(f"[OK] 已新增 {D_PATH}")
    for path in (A_PATH, B_PATH, C_PATH, D_PATH):
        py_compile.compile(path, doraise=True)
        print(f"[OK] 語法檢查通過 {path}")
    print("全部完成。")


if __name__ == "__main__":
    sys.exit(main())
