from dotenv import load_dotenv

load_dotenv()
import os
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

with driver.session() as session:
    # 撈出 priority = 1 的前 10 筆商品的名稱與 is_partner 狀態
    query = """
    MATCH (p:Product) 
    WHERE p.priority = 1 
    RETURN p.name AS name, p.is_partner AS is_partner, p.brand AS brand 
    LIMIT 10
    """
    result = session.run(query)
    
    print("\n🔍 【雲端 Neo4j 抽查結果：priority = 1 的商品狀態】")
    print("=" * 70)
    for record in result:
        print(f"📦 商品名稱: {record['name']}")
        print(f"   品牌: {record['brand']} | is_partner: {record['is_partner']}")
        print("-" * 70)

driver.close()
