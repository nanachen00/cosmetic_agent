"""
ZepMemoryAdapter 煙霧測試（Smoke Test）
只驗證 Zep 這條路是否真的打通，不牽涉 orchestrator / Neo4j。

執行方式：
    python3 tools/getzep/test_zep_memory_adapter.py
"""
import os
import sys
import time

base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from tools.getzep.zep_memory_adapter import ZepMemoryAdapter


def run_smoke_test():
    print("=" * 60)
    print("🧪 ZepMemoryAdapter 煙霧測試開始")
    print("=" * 60)

    test_user_id = "test_smoke_zep_001"

    # Step 1. 初始化（會自動建立 user + thread）
    print("\n[Step 1] 初始化 ZepMemoryAdapter...")
    try:
        memory = ZepMemoryAdapter(user_id=test_user_id)
        print(f"✅ 初始化成功 | user_id={memory.user_id} | thread_id={memory.thread_id}")
    except Exception as e:
        print(f"❌ 初始化失敗: {e}")
        return

    # Step 2. 測試結構化欄位 get（尚未寫入，應該是預設值）
    print("\n[Step 2] 測試 get()（尚未寫入的初始狀態）...")
    print(f"   skin_type: {memory.get('skin_type')}")
    print(f"   concerns: {memory.get('concerns')}")

    # Step 3. 測試 save()（寫入結構化欄位到 Zep User metadata）
    print("\n[Step 3] 測試手動寫入 profile 並 save()...")
    memory._profile["skin_type"] = "sensitive"
    memory._profile["concerns"] = ["保濕", "舒緩"]
    try:
        memory.save()
        print("✅ save() 呼叫成功（已寫入 Zep User metadata）")
    except Exception as e:
        print(f"❌ save() 失敗: {e}")
        return

    # Step 4. 重新建立一個新的 Adapter 實例，測試 metadata 是否真的持久化
    print("\n[Step 4] 重新建立新實例，驗證 metadata 是否成功持久化...")
    try:
        memory2 = ZepMemoryAdapter(user_id=test_user_id)
        print(f"   重新讀取 skin_type: {memory2.get('skin_type')}")
        print(f"   重新讀取 concerns: {memory2.get('concerns')}")
        if memory2.get("skin_type") == "sensitive":
            print("✅ [成功] Profile 持久化驗證通過！Zep User metadata 讀寫正常。")
        else:
            print("❌ [失敗] 重新讀取後 skin_type 不是剛剛寫入的值，metadata 沒有真的存進去。")
    except Exception as e:
        print(f"❌ 重新初始化失敗: {e}")
        return

    # Step 5. 測試 to_summary()
    print("\n[Step 5] 測試 to_summary()...")
    print(f"   {memory2.to_summary()}")

    # Step 6. 測試對話寫入（thread.add_messages）
    print("\n[Step 6] 測試 add_turn_to_zep()（寫入一輪對話）...")
    try:
        memory2.add_turn_to_zep(
            user_content="你好，我是敏感肌，很怕保養品含酒精。",
            assistant_content="了解！我會為您排除含酒精的產品。",
        )
        print("✅ 對話已成功寫入 Zep Thread")
    except Exception as e:
        print(f"❌ 對話寫入失敗: {e}")
        return

    # Step 7. 等待知識圖譜攝取，再測試 get_context_block()
    print("\n[Step 7] 等待知識圖譜攝取後，測試 get_context_block()...")
    print("⏳ 等待 8 秒讓 Zep 完成非同步攝取...")
    time.sleep(8)
    try:
        context = memory2.get_context_block()
        print("🧠 取得的 context block：")
        print(context if context else "（空字串）")
        if context:
            print("\n✅ [成功] get_context_block() 有正常回傳內容，Thread context 功能正常。")
        else:
            print("\n⚠️ [提示] context 是空的，可能是攝取還沒完成，可以再等久一點重跑這個測試。")
    except Exception as e:
        print(f"❌ get_context_block() 失敗: {e}")

    print("\n" + "=" * 60)
    print("🏁 煙霧測試結束")
    print("=" * 60)


if __name__ == "__main__":
    run_smoke_test()