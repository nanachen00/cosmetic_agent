"""
合規文案生成工具
Terra 生成 + Neo4j Regulation RAG
產出符合衛福部化妝品法規的推薦文案
"""
import sys
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')

import os
from app.clients.terra_completion_client import TerraCompletionClient
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()
TERRA_MODEL = os.getenv("TERRA_MODEL", "gpt-5.6-terra")
_terra_client = TerraCompletionClient.from_env()

_driver = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
    return _driver


def get_regulation_claims(category: str = None) -> dict:
    """
    從 Neo4j 查詢衛福部允許/禁用宣稱
    """
    driver = get_driver()
    result = {"allowed": [], "banned": []}

    with driver.session() as s:
        # 查允許宣稱（前10筆）
        r_allowed = s.run("""
            MATCH (reg:Regulation)
            WHERE reg.type = 'allowed_claim'
            RETURN reg.text AS text
            LIMIT 10
        """)
        result["allowed"] = [row["text"] for row in r_allowed if row["text"]]

        # 查禁用宣稱（前10筆）
        r_banned = s.run("""
            MATCH (reg:Regulation)
            WHERE reg.type = 'banned_claim'
            RETURN reg.text AS text
            LIMIT 10
        """)
        result["banned"] = [row["text"] for row in r_banned if row["text"]]

    return result


def generate_copy(
    product_name: str,
    category: str,
    ingredients: list,
    skin_type: str,
    concerns: list,
    warnings: list = None,
) -> str:
    """
    用 Terra 生成合規推薦文案

    參數：
        product_name: 商品名稱
        category: 保養品類別（面膜/精華液等）
        ingredients: 主要成分列表
        skin_type: 使用者膚質
        concerns: 使用者訴求
        warnings: 風險警告（若有，文案需含注意事項）

    回傳：2-3 句合規推薦文案
    """
    # 從 Neo4j 取法規宣稱
    claims = get_regulation_claims(category)
    allowed_str = "、".join(claims["allowed"][:5])
    banned_str  = "、".join(claims["banned"][:5])

    # 整理成分
    ing_str = "、".join(ingredients[:5]) if ingredients else "多種保養成分"

    # 整理膚質標籤
    skin_map = {
        "oily": "油性肌", "dry": "乾性肌",
        "sensitive": "敏感肌", "combination": "混合肌",
        "normal": "中性肌", "unknown": "各膚質",
    }
    skin_label = skin_map.get(skin_type, skin_type)
    concern_str = "、".join(concerns[:3]) if concerns else "日常保養"

    # 警告處理
    warning_note = ""
    if warnings:
        warning_note = f"\n注意事項（必須加入文案）：{'；'.join(warnings[:2])}"

    system_prompt = f"""你是台灣藥妝品牌的專業文案撰寫師，熟悉衛福部化妝品法規。

【衛福部允許宣稱詞句參考】：
{allowed_str}

【衛福部禁用宣稱詞句（絕對不可使用）】：
{banned_str}

【撰寫規則】：
1. 只能使用允許宣稱詞句，絕對不能使用禁用詞句
2. 不得出現「治療」「醫治」「消炎」「殺菌」「抗菌」「醫師推薦」等詞
3. 文案要親切、貼近台灣年輕女性語氣
4. 控制在 2 句話內、60 字以內，務必以句號或驚嘆號完整收尾
5. 必須符合使用者的膚質需求
6. 只輸出文案內容，不要加任何說明或前綴"""

    user_prompt = f"""請為以下保養品撰寫合規推薦文案：

商品：{product_name}
類別：{category}
主要成分：{ing_str}
適合膚質：{skin_label}
改善訴求：{concern_str}{warning_note}"""

    try:
        return _terra_client.complete(
            model=TERRA_MODEL,
            messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=220,
                temperature=0.4,
            timeout_seconds=180,
        )
    except Exception as e:
        print(f"[copywriting_tool] TAIDE 生成失敗：{e}")
        # fallback：用 allowed_claims 組簡單文案
        return f"蘊含{ing_str}，有效{concern_str}，適合{skin_label}日常使用。"


if __name__ == "__main__":
    print("=== copywriting_tool 測試 ===\n")

    # 測試1：乾肌保濕面膜
    copy1 = generate_copy(
        product_name="SEXYLOOK 玻尿酸保濕鎖水面膜",
        category="面膜",
        ingredients=["玻尿酸", "神經醯胺", "維他命B5"],
        skin_type="dry",
        concerns=["保濕", "補水"],
        warnings=[],
    )
    print(f"【乾肌面膜】\n{copy1}\n")

    # 測試2：敏感肌精華液（有警告）
    copy2 = generate_copy(
        product_name="Torriden 積雪草舒緩安瓶精華",
        category="精華液",
        ingredients=["積雪草", "泛醇", "尿囊素"],
        skin_type="sensitive",
        concerns=["舒敏", "修護"],
        warnings=["維他命C 不建議敏感肌使用"],
    )
    print(f"【敏感肌精華液】\n{copy2}\n")

    # 測試3：油肌洗面乳
    copy3 = generate_copy(
        product_name="LUCIDO 倫士度男士去油洗面乳",
        category="洗面乳",
        ingredients=["水楊酸", "甘草酸二鉀"],
        skin_type="oily",
        concerns=["控油", "抗痘"],
        warnings=[],
    )
    print(f"【油肌洗面乳】\n{copy3}\n")
