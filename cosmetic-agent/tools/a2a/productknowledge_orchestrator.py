# tools/agent/product_knowledge_orchestrator.py
"""
Product Agent → Knowledge Agent A2A 呼叫層
唯一目的：讓「商品分析結果」再補一層「知識庫安全性驗證」，
完全不修改 product_agent.py、knowledge_sub_agent.py、Root 任何一個既有檔案。

用法：
  原本 Root（或任何呼叫方）如果是這樣呼叫商品分析：
      from agents.product.product_agent import run as product_agent_run
      result = product_agent_run(intent, disliked_ingredients=disliked)

  只要改成呼叫這個新檔案，其餘完全不用動：
      from tools.agent.product_knowledge_orchestrator import run as product_agent_run
      result = product_agent_run(intent, disliked_ingredients=disliked)

  回傳格式跟原本 product_agent_run 完全一樣（products/graph_summary/warnings/
  total_candidates），只是 warnings 裡會多包含知識庫補強查到的風險警告。

測試請見同目錄下 test_product_knowledge_orchestrator.py。
"""

import os
import sys

# 🎯 強制把專案根目錄 /cosmetic-agent 塞進 Python 的搜尋小本本裡
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)
from schemas.intent import IntentParseResult

# 🎯 完全沿用既有的兩個 agent，不做任何修改，純粹當「使用者」去呼叫它們
from agents.product.product_agent import run as product_agent_run
from agents.knowledge.knowledge_sub_agent import run as knowledge_agent_run
from tools.a2a.product_knowledge_gate import product_knowledge_gate


def _call_knowledge_agent_for_safety(intent: IntentParseResult) -> dict:
    """
    商品分析 → 知識庫的 A2A 呼叫本體。
    先問 Gate 要不要問、要問才真的打 API，任何失敗都吞掉、不影響商品分析結果。
    """
    empty_result = {"warnings": [], "ingredient_info": None}

    gate_result = product_knowledge_gate.needs_knowledge_agent(intent.raw_query)
    print(f"🧭 [A2A Gate] {gate_result['reason']}")

    if not gate_result["decision"]:
        print("🧭 [A2A Gate] 判定不需要知識庫，略過這次 A2A 呼叫")
        return empty_result

    skin_type = intent.skin_type.value if intent.skin_type.value != "unknown" else "normal"
    print(f"📞 [A2A] 商品分析呼叫知識庫 Agent，query='{intent.raw_query}', skin_type='{skin_type}'")

    try:
        knowledge_result = knowledge_agent_run(
            user_query=intent.raw_query,
            is_a2a=True,
            user_skin_type=skin_type,
        )
    except Exception as e:
        print(f"❌ [A2A] 呼叫知識庫 Agent 發生例外，略過這次安全性補強：{e}")
        return empty_result

    if knowledge_result.get("status") != "success":
        print(f"⚠️ [A2A] 知識庫 Agent 回傳非 success：{knowledge_result.get('status')}")
        return empty_result

    payload = knowledge_result.get("payload", {})
    knowledge_warnings = payload.get("warnings", [])
    ingredient_info = payload.get("ingredient_info")

    if knowledge_warnings:
        print(f"🚨 [A2A] 知識庫回報 {len(knowledge_warnings)} 條風險警告")
    else:
        print("✅ [A2A] 知識庫檢查完畢，未發現額外風險")

    return {
        "warnings": knowledge_warnings,
        "ingredient_info": ingredient_info,
    }


def run(
    intent: IntentParseResult,
    disliked_ingredients: list = None,
    memory_context: str = "",
    preference_context: dict = None,
    excluded_product_ids: list = None,
) -> dict:
    """
    對外唯一入口，介面跟原本 product_agent_run 完全一致，可直接替換呼叫處。

    流程：
      1. 呼叫原本、未修改的 product_agent_run()，取得商品搜尋結果
      2. 呼叫知識庫 Agent 做補強安全性驗證（A2A，is_a2a=True）
      3. 合併兩邊的 warnings，回傳
    """
    # Step 1：原封不動呼叫既有的商品分析 Agent
    product_result = product_agent_run(
        intent,
        disliked_ingredients=disliked_ingredients,
        memory_context=memory_context,
        preference_context=preference_context,
        excluded_product_ids=excluded_product_ids,
    )

    # Step 2：A2A 呼叫知識庫 Agent 做補強驗證
    knowledge_safety = _call_knowledge_agent_for_safety(intent)
    knowledge_warnings = knowledge_safety.get("warnings", [])

    # Step 3：合併 warnings（去重），其餘欄位維持商品分析原本的結果
    merged_warnings = list(dict.fromkeys(
        list(product_result.get("warnings", [])) + knowledge_warnings
    ))

    return {
        **product_result,
        "warnings": merged_warnings,
        # 額外附加知識庫查到的成分資訊，方便上層需要時引用；
        # 不影響原本欄位結構，舊的呼叫方就算不知道這個新欄位也不會壞
        "knowledge_ingredient_info": knowledge_safety.get("ingredient_info"),
    }
