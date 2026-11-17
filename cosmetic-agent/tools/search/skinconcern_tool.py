# tools/search/skinconcern_tool.py
import math
import os
import sys
from neo4j import GraphDatabase
from app.clients.embedding_client import EmbeddingClient


class _SkinEmbeddingClient(EmbeddingClient):
    """Preserve the legacy explicit full endpoint while reusing client IO."""
    explicit_endpoint = None

    @property
    def endpoint(self):
        return self.explicit_endpoint or super().endpoint

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
        self.embedding_client = _SkinEmbeddingClient.from_env()
        self.embedding_client.explicit_endpoint = os.getenv("EMBEDDING_API_URL", "").strip() or None
        self.embedding_api_url = self.embedding_client.endpoint
        self.embedding_model = self.embedding_client.model
        self.embedding_timeout = self.embedding_client.timeout_seconds
        print("📡 [Skin Concern Tool] 雲端圖譜向量檢索器已成功就緒。")

    def _get_embedding(self, text):
        """Use the pooled, bounded-time client; never cache personal queries."""
        try:
            vector = self.embedding_client.embed_query(text)
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Nonfinite embedding")
            return vector
        except Exception as e:
            print(f"❌ 膚況向量轉換失敗: {type(e).__name__}")
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
