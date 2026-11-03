from tools.search.ingredient_tool import ingredient_tool

def run_qa_test():
    ingredient = "水楊酸"
    
    # 測試案例 1：純定義詢問
    q1 = "水楊酸是什麼？"
    print(f"--- 測試問題 1：{q1} ---")
    ans1 = ingredient_tool.lookup_and_explain(q1, ingredient)
    print(f"🤖 TAIDE 回答：\n{ans1}\n")

    # 測試案例 2：具體情境與安全性詢問
    q2 = "我每天晚上都擦水楊酸，這樣會太刺激嗎？"
    print(f"--- 測試問題 2：{q2} ---")
    ans2 = ingredient_tool.lookup_and_explain(q2, ingredient)
    print(f"🤖 TAIDE 回答：\n{ans2}\n")

if __name__ == "__main__":
    run_qa_test()
