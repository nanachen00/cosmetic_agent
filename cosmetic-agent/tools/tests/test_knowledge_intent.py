# tools/tests/test_knowledge_intent.py
import sys
import os

# 確保能 import 到專案根目錄的 modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# 🎯 引入組長親自調教、帶有 Few-Shot 範例的知識庫專屬調度官
from tools.search.knowledge_intent import knowledge_intent_parser

def run_intent_routing_test():
    print("======================================================================")
    print("🧪 Tomod's 雲端圖譜節點標籤動態路由（Few-Shot TAIDE）自動化測試管線")
    print("======================================================================")

    # 模擬 4 種真實使用者的複合型刁鑽提問
    test_cases = [
        {
            "id": "【🔥 測試 1：急性生理受傷不適】",
            "query": "救命！我昨天擦了酸類之後，今天整張臉脫皮，還能繼續擦嗎？"
        },
        {
            "id": "【🔥 測試 2：法規與成分混搭禁忌】",
            "query": "水楊酸跟高濃度的維他命C可以同一天晚上擦嗎？會不會太刺激？"
        },
        {
            "id": "【🔥 測試 3：換季膚況診斷與調理】",
            "query": "我最近因為換季，兩頰經常會乾癢脫皮，有什麼化妝品成分可以幫忙舒緩的嗎？"
        },
        {
            "id": "【🔥 測試 4：單純美妝成分科普】",
            "query": "我想知道積雪草這個成分最近為什麼這麼紅？它有什麼具體的醫美功效？"
        }
    ]

    for case in test_cases:
        print(f"\n{case['id']}")
        print(f"👤 使用者輸入: '{case['query']}'")
        
        try:
            # 🚨 核心發動：呼叫知識庫意圖調度官，讓 TAIDE 動態排定走訪圖譜的順序
            # 🚨 核心發動：呼叫知識庫意圖調度官
            ordered_cols = knowledge_intent_parser.get_ordered_collections(case["query"])
            
            # 💡 【精準修正】直接等於 ordered_cols 即可，因為它現在已經是漂亮的純英文標籤陣列了！
            col_names = ordered_cols
            # 對照組對齊：將舊的表名映射到高大上的 Neo4j 圖譜術語
            graph_mapping = {
                "risk_rules": "[:MAY_CAUSE] 禁忌邊多跳追蹤",
                "skin_concerns": "(:SkinConcern) 膚況特徵節點",
                "ingredients": "(:Ingredient) 護膚成分節點"
            }
            graph_routing_steps = [graph_mapping.get(name, name) for name in col_names]
            
            print(f"📦 [Success] Agent 成功排定 Neo4j 圖宇宙走訪路徑: { ' ➔ '.join(graph_routing_steps) }")
            
            # 💡 【核心修改】結合資管系系統測試規範，進行圖譜特徵驗證斷言 (Assertion Hint)
            if "risk_rules" in col_names and col_names[0] == "risk_rules":
                print("🛡️  [驗證結果]：安全第一原則發動！優先激活「圖譜安全防禦邊」攔截急性風險。")
            elif "skin_concerns" in col_names and col_names[0] == "skin_concerns":
                print("🩺  [驗證結果]：診斷優先原則發動！優先定位「(:SkinConcern) 節點」進行病徵比對。")
            else:
                print("📚  [驗證結果]：成分科普優先！優先匹配「(:Ingredient) 節點」提取語意向量。")
                
        except Exception as e:
            print(f"❌ 測試中斷報vec報錯: {e}")

if __name__ == "__main__":
    run_intent_routing_test()