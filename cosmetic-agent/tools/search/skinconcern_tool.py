# tools/search/skinconcern_tool.py
import requests
import os
import sys
from neo4j import GraphDatabase

# 確保引用路徑
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

class SkinConcernTool:
    def __init__(self):
        self.neo4j_uri = os.getenv("NEO4J_URI", "").strip()
        self.neo4j_user = os.getenv("NEO4J_USER", "").strip()
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "").strip()
        if not self.neo4j_uri or not self.neo4j_user or not self.neo4j_password:
            raise RuntimeError("缺少 NEO4J_USER 或 NEO4J_PASSWORD 環境變數")
        self.neo4j_driver = GraphDatabase.driver(
            self.neo4j_uri,
            auth=(self.neo4j_user, self.neo4j_password),
            keep_alive=True,
        )
        self.embedding_api_url = os.getenv(
            "EMBEDDING_API_URL",
            "http://localhost:8002/v1/embeddings",
        ).strip()
        self.embedding_model = os.getenv(
            "EMBEDDING_MODEL",
            "taide-12b",
        ).strip()
        self.embedding_timeout = float(
            os.getenv("EMBEDDING_TIMEOUT_SECONDS", "10")
        )
        print("📡 [Skin Concern Tool] 雲端圖譜向量檢索器已成功就緒。")

    def _get_embedding(self, text):
        """呼叫 8002 埠將用戶白話文症狀描述轉換為高維度語意向量"""
        
        # 🚀 規格對齊：將文字包進 List 陣列格式中，100% 滿足 8002 埠 `user_input[0]` 的提取期待
        payload = {
            "model": self.embedding_model,
            "input": [text]  # 👈 修正點 1：改成陣列型態！
        }
        
        try:
            # 🚀 修正點 2：給地端 CPU 模型多一點點運算包容時間（從 20 改為 60 秒）
            response = requests.post(
                self.embedding_api_url,
                json=payload,
                timeout=self.embedding_timeout,
            )
            
            if response.status_code == 200:
                return response.json()["data"][0]["embedding"]
            else:
                print(f"❌ [Embedding API Error] 狀態碼: {response.status_code} | 回應內容: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"❌ 膚況向量轉換連線失敗 (請確認 port 8002 是否開啟): {e}")
            return None

    def search_concern(self, user_query):
        """
        🎯 核心功能：利用 Neo4j 內建的 Vector Index 進行幾何空間語意搜尋！
        完美對齊官方 Vector Retriever 規格，解決白話文比對不到檔名（Name）的問題。
        """
        query_vector = self._get_embedding(user_query)
        if not query_vector:
            print("⚠️ 向量生成失敗，無法執行圖譜語意檢索。")
            return None

        # ============================================================
        # 🎯 關鍵 Cypher 演算法：調用 Neo4j 內建的向量查詢機制
        # ============================================================
        # 註：這裡的 "skin_concern_vector_index" 必須與妳在 Neo4j 上建立的索引名稱完全一致
        cypher_vector_search = """
        CYPHER 25
        MATCH (node:SkinConcern)
        SEARCH node IN (
            VECTOR INDEX skin_concern_vector_index
            FOR $query_vector
            LIMIT $top_k
        ) SCORE AS score
        RETURN node {.*} AS data, score
        LIMIT 1
        """
        
        with self.neo4j_driver.session() as session:
            try:
                res = session.run(
                    cypher_vector_search,
                    top_k=10,
                    query_vector=query_vector
                ).single()
                
                if res:
                    doc = res["data"]
                    score = res["score"]
                    print(f"✅ [Graph Vector Match] 成功語意匹配到膚況節點：{doc.get('name')} (餘弦相似度分數: {score:.4f})")
                    return doc
                else:
                    print(f"🔍 [Graph Vector] 雲端圖譜空間中找不到足夠相似的肌膚狀況。")
                    return None
            except Exception as e:
                print(f"❌ 雲端圖譜向量搜尋執行失敗 (請檢查 Neo4j 是否有建立 skin_concern_vector_index 索引): {e}")
                return None

    def __del__(self):
        """析構函式：程式關閉時自動斷開雲端連線，保護 AuraDB 免費額度不被卡死"""
        try:
            self.neo4j_driver.close()
        except:
            pass

# 實例化供 Agent 呼叫
skin_tool = SkinConcernTool()