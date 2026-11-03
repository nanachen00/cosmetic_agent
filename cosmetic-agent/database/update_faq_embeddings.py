from dotenv import load_dotenv

load_dotenv()
# tools/database/patch_seven_embeddings.py
import os
import sys
import time

# 強制全面採用 UTF-8 編碼，防止美妝文字中的特殊符號（如成分、符號）噴 ascii 錯誤
sys.stdout.reconfigure(encoding='utf-8')

from neo4j import GraphDatabase

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("❌ 偵測到未安裝 sentence-transformers 套件，請先執行: pip install sentence-transformers")
    sys.exit(1)

# 💡 延用妳原本的美妝專案設定
NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()

# 💡 精準指定妳剛才抓到那 7 個落單的內部元件 ID
TARGET_IDS = [293, 294, 295, 296, 297, 298, 299]

print("📡 [FAQ-BGE-M3 Patch] 啟動指定 7 筆美妝節點向量補登管線...")

# 1. 載入 BGE-M3 模型
try:
    print("⏳ 正在初始化本地 BGE-M3 模型...")
    model = SentenceTransformer('BAAI/bge-m3')
    print("✅ BGE-M3 模型載入成功！")
except Exception as e:
    print(f"❌ 載入 BGE-M3 模型失敗: {e}")
    sys.exit(1)

# 2. 連線 Neo4j
neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), keep_alive=True)

def patch_missing_embeddings():
    with neo4j_driver.session() as session:
        print(f"\n🔮 開始精準修復 ID 在 {TARGET_IDS} 的 7 個節點...")
        updated_count = 0
        
        for node_id in TARGET_IDS:
            # A. 根據內部 ID 撈出該 FAQEntry 的問題文字 (用 id(f) 匹配)
            # 這裡加上安全防護，只針對 FAQEntry 節點處理
            get_text_query = """
            MATCH (f:FAQEntry) 
            WHERE id(f) = $node_id 
            RETURN f.question AS question_text
            """
            result = session.run(get_text_query, node_id=node_id).single()
            
            if not result or not result["question_text"]:
                print(f" ⚠️ 警告：在 Neo4j 中找不到 ID {node_id} 的 FAQEntry 節點，或 question 欄位為空。")
                continue
                
            question_text = result["question_text"]
            print(f" ⏳ 偵測到 ID {node_id} ➔ 正在轉向量，問題：「{question_text}」")
            
            # B. 使用 BGE-M3 進行即時向量化
            try:
                faq_vector = model.encode(question_text, normalize_embeddings=True).tolist()
            except Exception as v_err:
                print(f" ❌ BGE-M3 向量計算發生錯誤 (ID: {node_id}): {v_err}")
                continue
                
            # C. 精準補登寫回
            update_result = session.run("""
                MATCH (f:FAQEntry) 
                WHERE id(f) = $node_id
                SET f.embedding = $embedding
                RETURN count(f) as updated_nodes
            """, node_id=node_id, embedding=faq_vector)
            
            summary = update_result.single()
            if summary and summary["updated_nodes"] > 0:
                print(f" ✅ [成功] ID {node_id} 向量注入完畢！")
                updated_count += 1
            else:
                print(f" ❌ [失敗] 無法更新 ID {node_id} 的節點。")
                
            # 微調緩衝，預防萬一
            time.sleep(0.2)
            
        print(f"\n🎉【精準修復完成】")
        print(f"🚀 預期處理 7 筆，實際成功補齊 {updated_count} 筆 FAQEntry 的 BGE-M3 向量！")

if __name__ == "__main__":
    try:
        patch_missing_embeddings()
    finally:
        neo4j_driver.close()