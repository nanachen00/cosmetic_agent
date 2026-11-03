# src/agents/root_agent.py
# StateGraph 主圖 + mock 節點
# B/C/D 完成後將 mock 替換為正式實作

from typing import Literal
from langgraph.graph import StateGraph, END

from models.state import AgentState


# ────────────────────────────────────────────────
# Mock 節點（B/C/D 完成後替換）
# ────────────────────────────────────────────────

def mock_intent_agent(state: AgentState) -> AgentState:
    """1-1 意圖解析 mock — B 完成後替換"""
    print("[mock] intent_agent running...")
    return {**state, "intent": "skin"}


def mock_copy_agent(state: AgentState) -> AgentState:
    """1-2 文案推薦 mock — B 完成後替換"""
    print("[mock] copy_agent running...")
    return {**state, "copy_text": "【mock】根據您的膚質，推薦以下保濕商品..."}


def mock_knowledge_agent(state: AgentState) -> AgentState:
    """2-1 藥妝知識 mock — C 完成後替換"""
    print("[mock] knowledge_agent running...")
    return {**state, "knowledge_results": [
        {"title": "乾性肌膚保養指南", "content": "建議使用含玻尿酸成分...", "score": 0.92},
    ]}


def mock_search_agent(state: AgentState) -> AgentState:
    """2-2 商品搜索 mock — D 完成後替換"""
    print("[mock] search_agent running...")
    return {**state, "product_results": [
        {"id": "P001", "name": "保濕精華液", "price": 580.0, "url": "https://example.com/P001"},
        {"id": "P002", "name": "玻尿酸面霜", "price": 750.0, "url": "https://example.com/P002"},
    ]}


# ────────────────────────────────────────────────
# 條件路由
# ────────────────────────────────────────────────

def route_by_intent(state: AgentState) -> Literal[
    "agent2_knowledge", "agent2_search", "end"
]:
    """
    意圖路由規則：
      skin     → agent2_knowledge（接著串 search）
      symptom  → agent2_knowledge（接著串 search）
      product  → agent2_search
      price    → agent2_search
      unknown  → end
    """
    intent = state.get("intent", "unknown")
    routing = {
        "skin":    "agent2_knowledge",
        "symptom": "agent2_knowledge",
        "product": "agent2_search",
        "price":   "agent2_search",
    }
    result = routing.get(intent, "end")
    print(f"[router] intent={intent} → {result}")
    return result


# ────────────────────────────────────────────────
# Root Agent 彙整節點
# ────────────────────────────────────────────────

def root_aggregate_node(state: AgentState) -> AgentState:
    """彙整所有結果，生成最終回覆（之後可換成 TAIDE 生成）"""
    knowledge = state.get("knowledge_results", [])
    products  = state.get("product_results", [])
    copy_text = state.get("copy_text", "")

    knowledge_str = "\n".join(
        f"- {k['title']}: {k['content']}" for k in knowledge
    ) or "（無知識庫結果）"

    products_str = "\n".join(
        f"- {p['name']} NT${p['price']}" for p in products
    ) or "（無商品結果）"

    final = (
        f"{copy_text}\n\n"
        f"【相關知識】\n{knowledge_str}\n\n"
        f"【推薦商品】\n{products_str}"
    )
    return {**state, "final_response": final}


# ────────────────────────────────────────────────
# StateGraph 主圖組裝
# ────────────────────────────────────────────────

def build_graph(
    intent_agent_fn=mock_intent_agent,
    copy_agent_fn=mock_copy_agent,
    knowledge_agent_fn=mock_knowledge_agent,
    search_agent_fn=mock_search_agent,
):
    """
    建立並回傳編譯好的 StateGraph。

    B/C/D 完成後傳入對應函數替換 mock：
        from agents.intent_agent    import run_intent_agent
        from agents.copy_agent      import run_copy_agent
        from agents.knowledge_agent import run_knowledge_agent
        from agents.search_agent    import run_search_agent

        app = build_graph(
            intent_agent_fn=run_intent_agent,
            copy_agent_fn=run_copy_agent,
            knowledge_agent_fn=run_knowledge_agent,
            search_agent_fn=run_search_agent,
        )
    """
    graph = StateGraph(AgentState)

    # 節點註冊
    graph.add_node("intent_agent",     lambda s: intent_agent_fn(s))
    graph.add_node("copy_agent",       lambda s: copy_agent_fn(s))
    graph.add_node("agent2_knowledge", lambda s: knowledge_agent_fn(s))
    graph.add_node("agent2_search",    lambda s: search_agent_fn(s))
    graph.add_node("aggregate",        root_aggregate_node)

    # 流程
    graph.set_entry_point("intent_agent")
    graph.add_edge("intent_agent", "copy_agent")

    graph.add_conditional_edges(
        "copy_agent",
        route_by_intent,
        {
            "agent2_knowledge": "agent2_knowledge",
            "agent2_search":    "agent2_search",
            "end":              END,
        }
    )

    # skin/symptom → knowledge → search → aggregate
    graph.add_edge("agent2_knowledge", "agent2_search")
    graph.add_edge("agent2_search",    "aggregate")
    graph.add_edge("aggregate",        END)

    return graph.compile()


# ────────────────────────────────────────────────
# 快速測試入口
# ────────────────────────────────────────────────

def run(user_input: str) -> AgentState:
    app = build_graph()
    initial_state: AgentState = {
        "user_input":        user_input,
        "intent":            "",
        "copy_text":         "",
        "knowledge_results": [],
        "product_results":   [],
        "final_response":    "",
        "user_history":      None,
        "recommendations":   None,
    }
    return app.invoke(initial_state)


#還沒建好資料vector database,且未連所以這只是一句mock
if __name__ == "__main__":
    result = run("我的皮膚很乾，有什麼推薦的保濕產品？")
    print("\n========== 最終回覆 ==========")
    print(result["final_response"])