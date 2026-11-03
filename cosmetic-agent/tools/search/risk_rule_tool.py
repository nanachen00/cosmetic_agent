# tools/search/risk_rule_tool.py
import os
import sys
from neo4j import GraphDatabase

# 確保引用路徑
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))
if base_path not in sys.path:
    sys.path.insert(0, base_path)

class RiskRuleTool:
    def __init__(self):
        # 對齊雲端 Neo4j AuraDB 資料庫連線
        self.neo4j_uri = os.getenv("NEO4J_URI", "").strip()
        self.neo4j_user = os.getenv("NEO4J_USER", "").strip()
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "").strip()
        if not self.neo4j_uri or not self.neo4j_user or not self.neo4j_password:
            raise RuntimeError("Neo4j configuration is incomplete")
        self.neo4j_driver = GraphDatabase.driver(
            self.neo4j_uri, 
            auth=(self.neo4j_user, self.neo4j_password), 
            keep_alive=True
        )
        print(f"📡 [Risk Rule Tool] 雲端圖譜強規則避險攔截器已就緒。")

    def check_ingredient_risk(self, session, ing_name: str, user_skin_type: str) -> list:
        """
        🎯 核心算法：在同一個資料庫 Session 中，發動 2-Hop 圖譜多跳路徑遍歷
        關係鏈路：(i:Ingredient) -[:APPLIES_TO_RULE]-> (r:RiskRule) -[:WARNS_CONCERN]-> (s:SkinConcern)
        """
        # 1. 膚質標籤對齊資料庫命名標準
        db_skin_type = "sensitive_skin" if user_skin_type in ["sensitive", "敏感肌", "sensitive_skin"] else user_skin_type
        
        # 2. 完美的 2-Hop 避險 Cypher 語法（無向量、純強規則硬核比對）
        cypher_risk = """
        MATCH (i:Ingredient)-[:APPLIES_TO_RULE]->(r:RiskRule)-[:WARNS_CONCERN]->(s:SkinConcern)
        WHERE (i.name_zh CONTAINS $ing_name OR $ing_name CONTAINS i.name_zh)
          AND (s.normalized_name = $skin_type OR r.conditions CONTAINS $skin_type)
        RETURN r.description AS description, 
               r.recommendation AS recommendation, 
               r.risk_level AS level,
               s.name AS condition_name
        """
        
        warnings_found = []
        try:
            records = session.run(cypher_risk, ing_name=ing_name, skin_type=db_skin_type)
            for rec in records:
                warn_msg = f"⚠️ 【圖譜硬核防禦 - {rec['level'].upper()} 風險】 {rec['description']}！藥師極其嚴肅建議：{rec['recommendation']}。"
                warnings_found.append(warn_msg)
        except Exception as e:
            print(f"❌ [Risk Tool Error] 執行圖譜強規則追蹤失敗: {e}")
            
        return warnings_found

    def __del__(self):
        """析構函式：優化 AuraDB 連線數"""
        try:
            self.neo4j_driver.close()
        except:
            pass

# 實例化供 Agent 呼叫
risk_rule_tool = RiskRuleTool()