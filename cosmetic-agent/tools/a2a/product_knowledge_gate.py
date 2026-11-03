# tools/a2a/product_knowledge_gate.py
class ProductKnowledgeGate:
    """
    🎯 核心功能：讓「商品分析 Agent」判斷，面對使用者這句話，
    需不需要跨 Agent 呼叫「知識庫 Agent」（is_a2a=True）去查成分/膚況/風險資料。

    只有明確的安全/禁忌問題才進知識庫。一般推薦不再多呼叫一層
    GPT/TAIDE gate，降低延遲並避免模型判斷互相覆蓋。
    """

    def __init__(self):
        self.safety_markers = (
            "安全嗎", "可以用嗎", "能用嗎", "適合我嗎", "會刺激嗎",
            "會不會刺激", "會過敏嗎", "副作用", "禁忌", "交互作用",
            "一起用嗎", "一起使用嗎", "混搭", "孕婦", "懷孕", "哺乳",
        )

    def needs_knowledge_agent(self, user_query: str) -> dict:
        """
        主要對外方法。
        回傳格式：{"decision": bool, "reason": str, "source": "rule"}
        - decision=True  → 商品分析 Agent 應該用 is_a2a=True 呼叫 KnowledgeSubAgent
        - decision=False → 不需要，商品分析 Agent 自己處理就好
        """
        normalized = str(user_query or "").strip()
        matched = [marker for marker in self.safety_markers if marker in normalized]
        decision = bool(matched)
        reason = (
            "明確安全問題：" + "、".join(matched[:3])
            if decision
            else "一般商品搜尋，不需要額外知識庫判斷"
        )
        return {"decision": decision, "reason": reason, "source": "rule"}

# 實例化工具，供商品分析 Agent 直接 import 使用
product_knowledge_gate = ProductKnowledgeGate()
