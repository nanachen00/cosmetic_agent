from dotenv import load_dotenv

load_dotenv()
import os
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

with driver.session() as session:
    # 找出所有剛匯入的商品，直接一鍵更正為 true
    query = "MATCH (p:Product {source_type: 'migrated'}) SET p.is_partner = true RETURN count(p) AS count"
    result = session.run(query)
    record = result.single()
    print(f"🎉 雲端修正大成功！已成功將 {record['count']} 筆剛匯入的商品 is_partner 全部更正為 TRUE！")

driver.close()
