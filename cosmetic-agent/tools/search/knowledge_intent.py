import requests
import json
import os

from openai import OpenAI
class KnowledgeIntentParser:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""),
        )

        self.model = "gpt-5.6-terra"

    def get_ordered_collections(self, user_query: str) -> list:
        """Choose graph collections without adding an LLM round trip by default."""
        mode = os.getenv(
            "KNOWLEDGE_COLLECTION_ROUTER",
            "deterministic",
        ).strip().lower()
        if mode in {"llm", "terra"}:
            ordered_names = self._ask_taide_for_order(user_query)
        else:
            ordered_names = self._route_by_rules(user_query)

        allowed = {"ingredients", "skin_concerns", "risk_rules"}
        final_routing_steps = []
        for name in ordered_names:
            if name in allowed and name not in final_routing_steps:
                final_routing_steps.append(name)
        return final_routing_steps or ["ingredients"]

    @staticmethod
    def _route_by_rules(user_query: str) -> list:
        query = (user_query or "").lower()
        danger_terms = (
            "救命", "脫皮", "爛臉", "刺痛", "紅腫", "副作用",
            "過敏", "能不能", "可以用", "注意", "禁忌", "混搭",
        )
        skin_terms = (
            "敏感肌", "油性肌", "乾性肌", "混合肌", "痘痘", "粉刺",
            "泛紅", "發癢", "乾癢", "受損", "毛孔", "膚況", "肌膚",
        )
        ingredient_terms = (
            "成分", "原料", "藥劑", "酸", "醇", "維他命", "玻尿酸",
            "菸鹼醯胺", "煙醯胺", "積雪草", "神經醯胺", "retinol",
            "niacinamide", "精華", "乳霜", "防曬", "保養品",
        )
        ordered = []
        if any(term in query for term in danger_terms):
            ordered.append("risk_rules")
        if any(term in query for term in skin_terms):
            ordered.append("skin_concerns")
        if any(term in query for term in ingredient_terms):
            ordered.append("ingredients")
        if not ordered:
            ordered.extend(("skin_concerns", "ingredients"))
        return ordered

    def _ask_taide_for_order(self, query: str) -> list:
        """
        🧠 頂級優化版：加入極詳細的『美妝藥劑實體觸發 Prompt』，逼 TAIDE 12B 做出完美的動態多表判斷
        """
        # 💡 提示詞保留妳們寫得非常完美的 Few-Shot（為了對齊系統，僅將 MongoDB 字眼稍微包裝）
        system_prompt = """妳是一位皮膚科藥劑師與 AI 系統調度專家。
        請根據用戶輸入的複合型問題，動態排定後端知識圖譜的檢索優先順序。
        
        妳能調度的圖宇宙標籤維度共有以下三個，其餘都不准杜撰：
        - ingredients: 成分/原料/藥劑知識（凡是涉及化學名、植物提取物、原料功效者皆須調用）。
        - skin_concerns: 肌膚症狀與狀況（凡是涉及皮膚當下的生理反應、膚質類別、受損情況者皆須調用）。
        - risk_rules: 風險、禁忌與安全避險（凡是涉及能不能用、副作用、混搭禁忌、急救避險者皆須調用）。

        🚨【🔥 終極嚴格：關鍵字實體與標籤映射規則】：
        在判定順序時，請將用戶問題切碎，只要問題中包含以下「任何一個」關鍵字眼，其對應的標籤『必須（MUST）』出現在回傳的清單中：

        1. 只要出現【成分/藥劑/原料】相關詞 ──► 必須包含 'ingredients'
           - 具體成分名：A醇(Retinol)、水楊酸、玻尿酸、維他命C、煙醯胺、積雪草、神經醯胺等。
           - 類別代名詞：酸類、醇類、保養品、原料、化學成分、藥劑、精華液、提取物。
           
        2. 只要出現【生理不適/膚況/症狀】相關詞 ──► 必須包含 'skin_concerns'
           - 生理症狀：脫皮、大刺痛、泛紅、紅腫、過敏、發癢、痘痘、粉刺、乾癢、受損。
           - 膚質類別：敏感肌、油性肌、乾性肌、混合肌、換季受損。
           
        3. 只要出現【安全性/禁忌/急迫性】相關詞 ──► 必須包含 'risk_rules'
           - 疑問與禁忌：能不能繼續擦、會不會爛臉、副作用、混搭、一起擦、同一天用、安全嗎、副作用。
           - 急迫性求助：救命、怎麼辦、完蛋了、完蛋、求救。

        🚨【🔥 資料表排序邏輯（三位一體原則）】：
        - 安全避險永遠置頂：只要偵測到包含第 3 點（risk_rules）的安全性字眼，'risk_rules' 必須強制排在第一位。
        - 症狀診斷接續：若同時包含第 2 點（skin_concerns），則將 'skin_concerns' 緊跟在後。
        - 知識保底殿後：若同時包含第 1 點（ingredients），則將 'ingredients' 補在最後面。

        【🔥 請嚴格參考以下真實調度案例（ Few-Shot 示範 ）】：
        
        案例 1：
        用戶問題：'救命！我昨天擦了酸類之後，今天整張臉脫皮，還能繼續擦嗎？'
        回傳：risk_rules, skin_concerns, ingredients
        
        案例 2：
        用戶問題：'水楊酸跟高濃度的維他命C可以同一天晚上擦嗎？會不會太刺激？'
        回傳：risk_rules, ingredients
        
        案例 3：
        用戶問題：'我最近因為換季，兩頰經常會乾癢脫皮，有什麼化妝品成分可以幫忙舒緩的嗎？'
        回傳：skin_concerns, ingredients
        
        案例 4：
        用戶問題：'我想知道積雪草這個成分最近為什麼這麼紅？它有什麼具體的醫美功效？'
        回傳：ingredients

        請直接回傳以逗號分隔的名稱字串。
        格式規範：只能回傳名稱（例如：'risk_rules, skin_concerns, ingredients''), 禁止任何解釋、禁止吐出任何中文字、贅字、標點符號（逗號除外）或問候語。"""
        try:
            response = (
                self.client
                .chat
                .completions
                .create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content":
                                system_prompt,
                        },
                        {
                            "role": "user",
                            "content":
                                f"用戶問題：'{query}'",
                        },
                    ],
                    max_completion_tokens=200,
                )
            )
        
            reply = (
                response
                .choices[0]
                .message
                .content
                or ""
            ).strip()
        
            if not reply:
                raise ValueError(
                    "OpenAI 沒有回傳路由結果"
                )
        
            clean_names = [
                name.strip().lower()
                for name in reply.split(",")
            ]
        
            print(
                "🚦 [OpenAI 路由決策] "
                f"{clean_names}"
            )
        
            return clean_names

        except Exception as error:
            print(
                "❌ OpenAI 路由判斷異常，"
                "走安全保底順序："
                f"{error}"
            )
        
            return [
                "risk_rules",
                "skin_concerns",
                "ingredients",
            ]
    
            

# 實例化工具
knowledge_intent_parser = KnowledgeIntentParser()