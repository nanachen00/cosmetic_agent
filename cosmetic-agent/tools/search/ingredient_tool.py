# tools/search/ingredient_tool.py

class IngredientLookupTool:
    """【任務 A】去 Neo4j 網子裡精準摸出成分球的基本常識身分證"""
    
    def get_node_data(self, session, keyword: str) -> dict:
        # 🎯 核心改動：由外部傳入現成的 session，不自己建立連線
        cypher = """
        MATCH (i:Ingredient)
        WHERE i.name_zh CONTAINS $keyword OR $keyword CONTAINS i.name_zh
        RETURN i {.*} AS data LIMIT 1
        """
        res = session.run(cypher, keyword=keyword).single()
        return res["data"] if res else None

# 實例化，讓外部直接 import 呼叫
ingredient_lookup_tool = IngredientLookupTool()
ingredient_tool = ingredient_lookup_tool