# test_intent.py
# 原本是 from tools.knowledge_intent import ...
from tools.search.knowledge_intent import knowledge_intent_parser as intent_parser

def test_router():
    test_cases = [
        "什麼是玻尿酸？",
        "水楊酸跟維他命C可以一起用嗎？",
        "我的皮膚最近紅癢脫皮",
        "我最近使用酸類產品，皮膚一直脫皮" # 這是關鍵的第四題！
    ]

    for q in test_cases:
        print(f"--- 測試問題：{q} ---")
        try:
            # 💡 修改點：使用複數型的 s，並用變數 cols 接收
            cols = intent_parser.get_collections_by_query(q)
            
            # 💡 因為回傳是 List，我們把名字都印出來
            col_names = [c.name for c in cols]
            print(f"✅ 結果：導向 {col_names} 資料表\n")
            
        except Exception as e:
            print(f"❌ 測試出錯：{e}\n")

if __name__ == "__main__":
    test_router()