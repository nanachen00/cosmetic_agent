import os
import sys
import traceback

# OPENAI_API_KEY must come from the runtime environment; never place a production key in tests.
os.environ["OPENAI_API_BASE"] = "http://localhost:8000/v1"

base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from agents.knowledge.knowledge_sub_agent import KnowledgeSubAgent


def print_dashboard(case_num, title, query, result, session_id=None):
    """專門用來打印詳細實體追蹤與處理細節的儀表板"""
    print("\n" + "="*80)
    print(f"📊 【測試案例 {case_num} 監控儀表板】: {title}")
    if session_id:
        print(f"🆔 Session ID: {session_id}")
    print(f"💬 用戶輸入白話文: '{query}'")
    print("="*80)
    print(f"🟢 執行狀態: {result.get('status')}")

    if result.get('status') == 'error':
        print(f"❌ 錯誤核心: {result.get('reply')}")
        print(f"🔍 崩潰 Stack Trace:\n{result.get('error')}")
        print("="*80 + "\n")
        return

    # 🚀 優化點 1：全方位相容層，包容所有可能的 JSON 封裝路徑
    payload = result.get('payload', {}) if 'payload' in result else result.get('debug_context', {})
    if not payload:
        payload = result  # 若沒有封裝，直接對齊最外層

    print("\n🧬 [資料庫實體與屬性追蹤 (Database Entity Properties)]")

    # 🚀 優化點 2：成分節點多 key 防禦性對帳，相容主程式的所有命名習慣
    ing = payload.get("ingredient_info") or payload.get("current_ingredient_data") or result.get("ingredient_data") or payload.get("ingredient")
    if ing:
        print(f"  🔸 標籤 (Label): :Ingredient (MAS 知識撈取成功！)")
        print(f"     ├── 實體中文名 (name_zh): {ing.get('name_zh', ing.get('name', '未定義'))}")
        print(f"     ├── 實體英文名 (name_en): {ing.get('name_en', '未定義')}")
        print(f"     ├── 原料類型 (ingredient_type): {ing.get('ingredient_type', '未定義')}")
        print(f"     └── 護膚效益陣列 (benefits): {ing.get('benefits', [])}")
    else:
        print("  🔸 標籤 :Ingredient ──► 本回合未調用或查無此成分")

    # 🚀 優化點 3：膚況節點多 key 防禦性對帳
    skin = payload.get("skin_concern_info") or payload.get("current_concern_data") or result.get("skin_concern_data") or payload.get("skin_concern")
    if skin:
        print(f"  🔸 標籤 (Label): :SkinConcern (首跳向量語意召回命中！)")
        print(f"     ├── 症狀名稱 (name): {skin.get('name', skin.get('name_zh'))}")
        print(f"     ├── 歸屬類別 (category): {skin.get('category')}")
        print(f"     ├── 歸一化標籤 (normalized_name): {skin.get('normalized_name')}")
        print(f"     └── 臨床症狀特徵處理 (common_signs): {skin.get('common_signs', [])[:3]}... (已清洗其餘)")
    else:
        print("  🔸 標籤 :SkinConcern ──► 本回合未調用或未觸發向量匹配")

    # 2. 追蹤圖譜多跳避險處理
    print("\n🛡️ [圖譜關係線拓撲與避險處理 (Graph Edge & Guardrail)]")
    warnings = payload.get("warnings", []) or result.get("warnings", [])
    if warnings:
        print(f"  🚨 避險狀態: 🔴 觸發大閘門攔截 (Triggered) ── 發現 {len(warnings)} 條實體碰撞！")
        print(f"  🎬 圖譜處理邏輯: 經由 [:APPLIES_TO_RULE] ──► 激活 (:RiskRule) ──► 經由 [:WARNS_CONCERN] ──► 鎖定阻斷")
        for i, warn in enumerate(warnings, 1):
            print(f"     ├── 攔截警告 {i}: {warn}")
    else:
        print("  🚨 避險狀態: 🟢 安全放行 (Cleared) ── 2-Hop 圖譜多跳推理完畢，未發現紅線對撞")

    # 3. 輸出生成層
    if 'reply' in result:
        print("\n✍️  [大模型 Answer Generator 最終渲染層]")
        print(f"  🎬 處理邏輯: 提取上方硬核證據字串 ──► 注入 System Prompt ──► 扣鎖 TAIDE 12B 自我審查")
        print(f"  💬 藥劑師顧問回覆:\n{result.get('reply')}")

    print("="*80 + "\n")


def run_knowledge_agent_test_suite():
    print("=" * 60)
    print("🧪 Tomod's 知識庫 Agent 自動化深度測試套件 (Observability Verifier)")
    print("=" * 60)

    agent = KnowledgeSubAgent()

    # 🎯 測試 1：單輪查詢（沿用原本的三個案例，驗證基本功能沒壞掉）
    query_1 = "我想知道維他命C的功效，還有敏感肌可以用嗎？"
    try:
        result_1 = agent.handle_request(user_query=query_1, is_a2a=False, user_skin_type="sensitive")
        print_dashboard(1, "Root 直接分派 - 查詢成分功效與安全性", query_1, result_1)
    except Exception:
        print_dashboard(1, "Root 直接分派", query_1, {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()})

    # 🎯 測試 2：A2A 模式（保持原樣，A2A 不涉及對話記憶）
    query_2 = "水楊酸與痤瘡桿菌"
    try:
        result_2 = agent.handle_request(user_query=query_2, is_a2a=True, user_skin_type="oily")
        print_dashboard(2, "A2A 跨組協作 - 商品分析大腦要查原料做硬規則比對", query_2, result_2)
    except Exception:
        print_dashboard(2, "A2A 跨組協作", query_2, {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()})

    # 🎯 測試 3：壓力測試（保持原樣）
    query_3 = "救命，我擦了A醇之後整張臉大刺痛紅腫過敏，該怎麼辦？"
    try:
        result_3 = agent.handle_request(user_query=query_3, is_a2a=False, user_skin_type="sensitive")
        print_dashboard(3, "壓力與安全性測試 - 嚴重刺痛紅腫的複合意圖", query_3, result_3)
    except Exception:
        print_dashboard(3, "壓力與安全性測試", query_3, {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()})

    # ============================================================
    # 🎯【新增】測試 4：多輪對話記憶測試
    # 用同一個 session_id 連續問三句話，驗證：
    #   4a. 第一句正常查成分
    #   4b. 第二句是短的追問句「那敏感肌呢？」，單獨看沒有任何實體關鍵字，
    #       驗證系統能不能靠上一句歷史 + 指代消解，正確查到 risk_rules
    #   4c. 第三句換一個話題，驗證歷史會不會被之前的內容干擾、混淆
    # ============================================================
    print("\n" + "#" * 60)
    print("🧪 【多輪對話記憶測試組】")
    print("#" * 60)

    session_id_multiturn = "test_user_alice"

    # --- 4a: 開場，問水楊酸 ---
    query_4a = "水楊酸有什麼功效？"
    try:
        result_4a = agent.handle_request(
            user_query=query_4a,
            is_a2a=False,
            user_skin_type="oily",
            session_id=session_id_multiturn,
        )
        print_dashboard(
            "4a", "多輪對話 - 第一輪：詢問水楊酸功效",
            query_4a, result_4a, session_id=session_id_multiturn
        )
    except Exception:
        print_dashboard(
            "4a", "多輪對話 - 第一輪", query_4a,
            {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()},
            session_id=session_id_multiturn
        )

    # --- 4b: 追問句，單獨看完全沒有實體關鍵字，考驗多輪記憶 + 指代消解 ---
    query_4b = "那敏感肌呢？"
    try:
        result_4b = agent.handle_request(
            user_query=query_4b,
            is_a2a=False,
            user_skin_type="sensitive",
            session_id=session_id_multiturn,
        )
        print_dashboard(
            "4b", "多輪對話 - 第二輪：短追問句（考驗記憶與指代消解）",
            query_4b, result_4b, session_id=session_id_multiturn
        )
        # 額外驗證：回覆內容裡應該還是有提到水楊酸相關字眼，代表模型真的記得上文
        reply_text = result_4b.get("reply", "")
        if "水楊酸" in reply_text or "酸" in reply_text:
            print("✅ [記憶驗證] 回覆內容中偵測到延續上文的成分字眼，記憶功能運作正常！")
        else:
            print("⚠️ [記憶驗證] 回覆內容中沒有偵測到延續上文的成分字眼，建議人工檢查 prompt 或歷史是否正確傳遞。")
    except Exception:
        print_dashboard(
            "4b", "多輪對話 - 第二輪", query_4b,
            {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()},
            session_id=session_id_multiturn
        )

    # --- 4c: 換話題，驗證歷史不會造成混淆、也不會被清空 ---
    query_4c = "另外我想問維他命C可以跟水楊酸一起用嗎？"
    try:
        result_4c = agent.handle_request(
            user_query=query_4c,
            is_a2a=False,
            user_skin_type="sensitive",
            session_id=session_id_multiturn,
        )
        print_dashboard(
            "4c", "多輪對話 - 第三輪：換話題但仍在同一 session",
            query_4c, result_4c, session_id=session_id_multiturn
        )
    except Exception:
        print_dashboard(
            "4c", "多輪對話 - 第三輪", query_4c,
            {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()},
            session_id=session_id_multiturn
        )

    # --- 4d: 驗證不同 session_id 之間互不干擾 ---
    session_id_other = "test_user_bob"
    query_4d = "那敏感肌呢？"  # 跟 4b 一模一樣，但這是全新的 session，理論上應該查不到上文
    try:
        result_4d = agent.handle_request(
            user_query=query_4d,
            is_a2a=False,
            user_skin_type="sensitive",
            session_id=session_id_other,
        )
        print_dashboard(
            "4d", "多輪對話 - 隔離測試：全新 session 問同一句追問句",
            query_4d, result_4d, session_id=session_id_other
        )
        print("💡 [隔離驗證說明] 這位是全新客人 Bob，跟 Alice 的對話應完全獨立。")
        print("   如果這裡的回覆仍然提到水楊酸，代表 session 隔離出了問題，請檢查 conversation_history 的 key 邏輯。")
    except Exception:
        print_dashboard(
            "4d", "多輪對話 - 隔離測試", query_4d,
            {"status": "error", "reply": "測試腳本層面崩潰", "error": traceback.format_exc()},
            session_id=session_id_other
        )

    # --- 4e: 印出 Alice 的完整對話歷史，人工檢查記憶是否正確累積 ---
    print("\n" + "-" * 60)
    print("🗂️ [對話歷史檢查] Alice (test_user_alice) 目前累積的完整歷史：")
    print("-" * 60)
    alice_history = agent.conversation_history.get(session_id_multiturn, [])
    for i, msg in enumerate(alice_history, 1):
        role_label = "👤 客人" if msg["role"] == "user" else "💊 藥劑師"
        print(f"  {i}. {role_label}: {msg['content'][:60]}{'...' if len(msg['content']) > 60 else ''}")
    print("-" * 60 + "\n")


if __name__ == "__main__":
    run_knowledge_agent_test_suite()