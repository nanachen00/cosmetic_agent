# tools/agent/test_product_knowledge_orchestrator.py
"""
product_knowledge_orchestrator.py 的測試腳本。

執行方式：
    python3 tools/agent/test_productknowledge_orchestrator.py
"""
import os
import sys

base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from schemas.intent import (
    IntentParseResult, IntentType, SkinType,
    ProductCategory, TexturePreference
)
from tools.a2a.productknowledge_orchestrator import run


def run_test_suite():
    print("=" * 60)
    print("Product Agent A2A 測試（不修改任何既有 agent）")
    print("=" * 60)

    print("\n=== 測試 1：不含具體成分，Gate 應判定不需要知識庫 ===")
    r1 = run(IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.DRY,
        concern=['保濕'],
        product_category=ProductCategory.MASK,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['保濕', '補水', '鎖水', '乾燥', '面膜'],
        raw_query='乾肌找保濕面膜',
        ambiguity_score=0.1,
    ))
    print(f"找到 {len(r1['products'])} 筆")
    print(f"警告：{r1['warnings']}")

    print("\n=== 測試 2：這罐含水楊酸的精華液適合敏感肌嗎（應觸發 A2A）===")
    r2 = run(IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.SENSITIVE,
        concern=['舒敏', '保濕'],
        product_category=ProductCategory.SERUM,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['水楊酸', '精華液'],
        raw_query='這罐含水楊酸的精華液適合敏感肌嗎',
        ambiguity_score=0.1,
    ), disliked_ingredients=['維他命C'])
    print(f"找到 {len(r2['products'])} 筆（已過濾維他命C）")
    print(f"警告（含知識庫補強）：{r2['warnings']}")
    print(f"知識庫查到的成分資訊：{r2['knowledge_ingredient_info']}")


if __name__ == "__main__":
    run_test_suite()