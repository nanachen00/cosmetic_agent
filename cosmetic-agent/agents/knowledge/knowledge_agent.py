# agents/knowledge/knowledge_agent.py
import sys
import os
from app.clients.terra_completion_client import TerraCompletionClient

base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

from tools.search.graph_search import graph_search, format_graph_result
from tools.search.ingredient_tool import ingredient_tool
from tools.search.skinconcern_tool import skin_tool
from tools.agent.intent_parser import parse_intent
from tools.search.knowledge_intent import knowledge_intent_parser


class KnowledgeSubAgent:
    def __init__(self):
        self.ingredient_tool = ingredient_tool
        self.skin_tool = skin_tool
        self.model = os.getenv("TERRA_MODEL", "gpt-5.6-terra")
        self.client = TerraCompletionClient.from_env()

    def handle_request(self, user_query: str, is_a2a: bool = False, user_skin_type: str = "normal") -> dict:
        print(f"\n🤖 [Knowledge Agent] 收到請求: '{user_query}' | A2A模式={is_a2a} | 膚質={user_skin_type}")

        final_context = {
            "graph_raw_data": None,
            "ingredient_info": None,
            "skin_concern_info": None,
            "warnings": []
        }

        intent_res = parse_intent(user_query=user_query, model=self.model)
        ordered_collections = knowledge_intent_parser.get_ordered_collections(user_query)

        for col in ordered_collections:
            print(f"🔄 [MAS Pipeline] 依序激活資料表檢索任務: {col.name}")

            if col.name == "ingredients":
                if intent_res and intent_res.query_expansion:
                    search_keyword = intent_res.query_expansion[0]
                else:
                    search_keyword = user_query
                ing_data = self.ingredient_tool.vector_search_ingredient(search_keyword)
                if ing_data:
                    final_context["ingredient_info"] = ing_data

            elif col.name == "skin_concerns":
                if intent_res and intent_res.concern:
                    primary_concern = intent_res.concern[0]
                else:
                    primary_concern = user_query
                skin_data = self.skin_tool.search_concern(primary_concern)
                if skin_data:
                    final_context["skin_concern_info"] = skin_data

            elif col.name == "risk_rules":
                try:
                    graph_res = graph_search(product_ids=[], user_skin_type=user_skin_type)
                    final_context["graph_raw_data"] = graph_res
                    if graph_res and graph_res.get('warnings'):
                        final_context["warnings"].extend(graph_res['warnings'])
                except Exception as graph_err:
                    print(f"   ⚠️ [Graph Warning] 圖譜多跳追蹤跳過或暫無資料: {graph_err}")

        if is_a2a:
            return {
                "status": "success",
                "payload": final_context
            }
        else:
            return self._generate_final_pharmacist_reply(user_query, final_context)

    def _generate_final_pharmacist_reply(self, query: str, context: dict) -> dict:
        evidence_str = ""
        if context.get("ingredient_info"):
            evidence_str += f"\n成分知識：{context['ingredient_info'].get('ingredient_name_zh', '未知成分')} 主要功效為 {context['ingredient_info'].get('efficacy', '無')}。"
        if context.get("graph_raw_data"):
            evidence_str += f"\n圖譜追蹤警告：{context['graph_raw_data'].get('warnings', '無特殊禁忌')}"
        # 防呆：查無資料時給模型明確指引
        if not evidence_str:
            evidence_str = "本次查無相關成分或圖譜資料，請依一般藥妝專業知識回答。"
            
        system_prompt = f"""妳是一位藥妝顧問。請結合以下後台數據庫的【硬核證據】，用溫柔、具備說服力的「繁體中文」回答使用者的藥妝知識問題。

【硬核證據】：{evidence_str}

【限制條件】：
1. 必須嚴格遵守證據中的禁忌警告，若有風險必須明確提醒！
2. 不要提到「數據庫顯示」、「邊1」等技術術語，要自然融入回答中。
3. 不要用專業術語，要使用淺顯易懂的詞語，例如不使用抗氧化、自由基等專業名詞
【輸出格式】：
1.以列點形式來呈現
"""


        try:
            reply_text = self.client.complete(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                max_tokens=600,
                temperature=0.4,
                timeout_seconds=20,
            )
            return {
                "status": "success",
                "reply": reply_text,
                "stage": "knowledge_agent_final_reply"
            }
        except Exception as e:
            return {
                "status": "error",
                "reply": "不好意思，我剛剛查閱成分庫時出了一點小盲點，請妳再問我一次～",
                "error": str(e)
            }


# ── run wrapper（供 orchestrator 呼叫，在 class 外面）──
_agent_instance = None


def run(user_query: str, is_a2a: bool = False, user_skin_type: str = "normal") -> dict:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = KnowledgeSubAgent()
    return _agent_instance.handle_request(
        user_query=user_query,
        is_a2a=is_a2a,
        user_skin_type=user_skin_type,
    )