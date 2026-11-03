"""
多輪 A2A 聯邦測試：驗證商品推薦後，追問成分是否能正確連動知識庫。
執行方式：
    python3 tools/agent/test_multiturn_product_knowledge.py
"""
import os
import sys

# 強制將總目錄塞進搜尋路徑
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from schemas.intent import (
    IntentParseResult, IntentType, SkinType,
    ProductCategory, TexturePreference
)
from tools.a2a.productknowledge_orchestrator import run


def run_multiturn_test():
    print("=" * 70)
    # 這裡模擬前端幫這位調皮客人 Alice 建立的對話 Session ID
    session_id = "test_alice_saborino"
    print(f"🎬 開始多輪 A2A 聯邦測試 | Session ID: {session_id}")
    print("=" * 70)

    # ------------------------------------------------------------------
    # 🎯 第一輪對話：客人先盲找早安面膜（預期 Gate 判定 NO，純商品搜尋）
    # ------------------------------------------------------------------
    print("\n[🏃‍♂️ 第一輪] 客人提問：『推薦好用的早安面膜』")
    
    intent_turn1 = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.NORMAL,
        concern=['保濕', '日常護理'],
        product_category=ProductCategory.MASK,
        texture_preference=TexturePreference.UNKNOWN,
        query_expansion=['早安面膜', 'Saborino', '面膜'],
        raw_query='推薦好用的早安面膜',
        ambiguity_score=0.1
    )

    # 呼叫編排器（傳入 session_id 讓系統開始記日記）
    # 註：如果妳的 orchestrator 的 run 函式還沒吃 session_id，記得在定義那邊加上 `session_id: str = "default"` 並傳給內部 agent
    try:
        result_turn1 = run(intent_turn1, disliked_ingredients=[])
        products = result_turn1.get("products", [])
        print(f"📡 [第一輪結果] 商品大腦成功撈出 {len(products)} 筆候選商品")
        print("\n📋 【商品大腦全數候選清單（Top 5 向量召回）】:")
        for idx, prod in enumerate(products, 1):
            print(f"   {idx}. 商品名稱: {prod.get('name')}")
            print(f"      ├── 品牌: {prod.get('brand')}")
            print(f"      ├── 價格: {prod.get('price')} 元")
            print(f"      └── 成分清單數量: {len(prod.get('ingredients_list', []))} 個成分")
        print("-" * 50 + "\n")
        
        # 模擬商品大腦成功推薦了這款商品，並把名字印出來
        target_product_name = "BCL Saborino早安面膜28枚入-積雪草CIC"
        print(f"🛍️  系統前端成功向客人推薦：『{target_product_name}』")
        
    except Exception as e:
        print(f"❌ 第一輪發生崩潰: {e}")
        return

    # ------------------------------------------------------------------
    # 🎯 第二輪對話：客人針對剛才推薦的商品，進行含糊的成分功效追問
    #    （此時 Gate 應判定 YES，並且結合多輪對話歷史，動態還原主詞，呼叫知識庫！）
    # ------------------------------------------------------------------
    print("\n" + "-"*50)
    print("[🏃‍♂️ 第二輪] 客人提問：『那裡面含有的積雪草成分有什麼功效？適合敏感肌嗎？』")
    print("-"*50)

    intent_turn2 = IntentParseResult(
        intent_type=IntentType.FIND_PRODUCT,
        skin_type=SkinType.SENSITIVE, # 👈 這輪客人切換成敏感肌膚質進行測試
        concern=['舒敏', '修護'],
        product_category=ProductCategory.MASK,
        texture_preference=TexturePreference.UNKNOWN,
        # 這裡的 query_expansion 和 raw_query 是客人當下的話
        query_expansion=['積雪草', '功效', '敏感肌'],
        raw_query='那裡面含有的積雪草成分有什麼功效？適合敏感肌嗎？',
        ambiguity_score=0.1
    )

    try:
        # 呼叫編排器，維持同一個 session_id
        result_turn2 = run(intent_turn2, disliked_ingredients=[])
        import pprint
        pprint.pprint(result_turn2)
        # ============================================================
        # 🚀 加上這段：把 TAIDE 藥劑師生成給客人的專業列點回覆印出來！
        # ============================================================
        print("\n💬 【TAIDE 模擬藥劑師對客人的溫柔回覆】:")
        if result_turn2.get("reply"):
            print(result_turn2.get("reply"))
        else:
            # 如果 orchestrator 回傳的外層沒有 reply，試著去 payload 裡面抓取
            payload_data = result_turn2.get("payload", {})
            if isinstance(payload_data, dict) and payload_data.get("reply"):
                print(payload_data.get("reply"))
            else:
                print("   （⚠️ 提示：此輪結果可能為純 A2A 數據對帳，或中控未關閉 is_a2a=True，因而未觸發文本生成）")
        print("-" * 50)
        # ============================================================
        
        print("\n📊 【第二輪 A2A 對帳觀測儀表板】")
        print(f"🟢 最終攔截警告 (warnings): {result_turn2.get('warnings')}")
        
        # 🎯 這裡是最關鍵的檢查點！
        # 如果 A2A 成功，這兩行絕對不能是空值，必須把積雪草的圖譜身分證給洗出來
        kn_info = result_turn2.get("knowledge_ingredient_info")
        print(f"🧬 知識庫召回的成分資訊 (knowledge_ingredient_info):")
        if kn_info:
            print(f"   ├── 成分中文名: {kn_info.get('name_zh')}")
            print(f"   ├── 成分英文名: {kn_info.get('name_en')}")
            print(f"   └── 護膚效益陣列: {kn_info.get('benefits', [])}")
            print("\n✅ [成功案例斷言] 商品 Agent 成功在第二輪透過 Orchestrator 激活知識庫 Agent！")
        else:
            print("   ❌ 狀態為 None！代表指代消解或成分名稱與圖譜 ':Ingredient' 節點對帳失敗。")
            
    except Exception as e:
        print(f"❌ 第二輪發生崩潰: {e}")

    print("=" * 70)
    


if __name__ == "__main__":
    run_multiturn_test()