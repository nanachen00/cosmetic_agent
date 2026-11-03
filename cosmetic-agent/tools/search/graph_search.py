"""
Knowledge Graph RAG 查詢 — Neo4j AuraDB 版本（橋接兩種 Ingredient 節點）

節點類型：
  類型A：爬蟲成分（name / name_en / normalized_name）← Product -[CONTAINS]-> 這裡
  類型B：知識庫成分（name_zh / name_en / avoid_for / risks）← 有 APPLIES_TO_RULE 等關係

橋接方式：用 name_en 做 JOIN
"""
from dotenv import load_dotenv

load_dotenv()
import os
import sys
sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')

from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "").strip()

_driver = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
    return _driver

SKIN_TYPE_TO_AVOID = {
    'sensitive':   ['sensitive_skin', 'irritated_skin', 'very_sensitive_skin'],
    'dry':         ['dry_skin', 'very_dry_skin'],
    'oily':        ['oily_skin', 'acne_prone_skin'],
    'combination': ['oily_skin'],
    'normal':      [],
}

SKIN_TYPE_TO_CONCERN_ZH = {
    'sensitive':   ['敏感肌', '刺激性接觸性皮膚炎'],
    'dry':         ['乾燥肌膚', '缺水型肌膚'],
    'oily':        ['皮脂分泌過多', '毛孔粗大'],
    'combination': ['皮脂分泌過多'],
    'normal':      [],
}


def graph_search(product_ids: list, user_skin_type: str = None) -> dict:
    result = {
        'products':          [],
        'ingredients':       [],
        'risks':             [],
        'skin_concerns':     [],
        'suitable_concerns': [],
        'warnings':          [],
        'product_suits':     [],
        'risk_warns':        [],
        'regulations':       [],
        'conflicts':         [],
    }

    if not product_ids:
        return result

    driver = get_driver()

    with driver.session() as s:

        # ── 邊1：Product → 類型A Ingredient（CONTAINS）──
        # 取得爬蟲成分的 name_en，作為橋接 key
        r1 = s.run("""
            MATCH (p:Product)-[:CONTAINS]->(i:Ingredient)
            WHERE elementId(p) IN $ids
          AND coalesce(p.recommendable, true) = true
            RETURN
                elementId(p)                        AS product_id,
                p.name                              AS product_name,
                p.category                          AS category,
                elementId(i)                        AS ingredient_id_a,
                coalesce(i.name_zh, i.name, i.name_en) AS ingredient_name_zh,
                toLower(i.name_en)                  AS name_en_lower
            ORDER BY product_id
        """, ids=product_ids)

        name_en_lowers = []
        for row in r1:
            d = dict(row)
            result['ingredients'].append(d)
            if d['name_en_lower'] and d['name_en_lower'] not in name_en_lowers:
                name_en_lowers.append(d['name_en_lower'])

        if not name_en_lowers:
            return result

        # ── 橋接：用 name_en 找類型B Ingredient 的 elementId ──
        r_bridge = s.run("""
            MATCH (i:Ingredient)
            WHERE i.name_zh IS NOT NULL
              AND toLower(i.name_en) IN $name_en_lowers
            RETURN
                elementId(i)        AS ingredient_id_b,
                i.name_zh           AS name_zh,
                i.name_en           AS name_en,
                i.avoid_for         AS avoid_for,
                i.suitable_for      AS suitable_for,
                i.risks             AS risks,
                toLower(i.name_en)  AS name_en_lower
        """, name_en_lowers=name_en_lowers)

        kb_ingredients = [dict(r) for r in r_bridge]
        kb_ids = [i['ingredient_id_b'] for i in kb_ingredients]

        # 更新 ingredients 的 name_zh（用橋接結果補齊）
        kb_map = {i['name_en_lower']: i for i in kb_ingredients}
        for ing in result['ingredients']:
            matched = kb_map.get(ing.get('name_en_lower', ''))
            if matched:
                ing['ingredient_name_zh'] = matched['name_zh']

        # ── 直接從 avoid_for 判斷膚質警告 ──
        if user_skin_type:
            user_avoids = SKIN_TYPE_TO_AVOID.get(user_skin_type, [])
            for ing in kb_ingredients:
                avoid_list = ing.get('avoid_for') or []
                for av in user_avoids:
                    if av in avoid_list:
                        msg = f"{ing['name_zh']} 不建議{user_skin_type}膚質使用"
                        if msg not in result['warnings']:
                            result['warnings'].append(msg)
                        break

        if not kb_ids:
            # 沒有橋接到知識庫成分，直接回傳基本結果
            return result

        # ── 邊2+3：類型B Ingredient → RiskRule → SkinConcern ──
        r2 = s.run("""
            MATCH (i:Ingredient)-[:APPLIES_TO_RULE]->(rr:RiskRule)-[:WARNS_CONCERN]->(sc:SkinConcern)
            WHERE elementId(i) IN $ids
            RETURN
                i.name_zh                           AS ingredient,
                rr.description                      AS description,
                coalesce(rr.risk_level, 'medium')   AS risk_level,
                coalesce(rr.recommendation, '')     AS recommendation,
                collect(DISTINCT sc.name)           AS warned_concerns
        """, ids=kb_ids)

        for row in r2:
            d = dict(row)
            result['risk_warns'].append(d)
            result['risks'].append({
                'ingredient':  d['ingredient'],
                'description': d['description'],
                'risk_level':  d['risk_level'],
            })
            if user_skin_type:
                concern_targets = SKIN_TYPE_TO_CONCERN_ZH.get(user_skin_type, [])
                if any(c in d['warned_concerns'] for c in concern_targets):
                    if d['risk_level'] in ('medium', 'high'):
                        msg = f"[風險] {d['description']}：{d['recommendation']}"
                        if msg not in result['warnings']:
                            result['warnings'].append(msg)

        # ── 邊5：類型B Ingredient → SkinConcern（SUITABLE_FOR_CONCERN）──
        r5 = s.run("""
            MATCH (i:Ingredient)-[:SUITABLE_FOR_CONCERN]->(sc:SkinConcern)
            WHERE elementId(i) IN $ids
            RETURN
                i.name_zh                   AS ingredient,
                collect(DISTINCT sc.name)   AS suitable_concerns
        """, ids=kb_ids)

        for row in r5:
            result['suitable_concerns'].append(dict(row))

        # ── 邊4：商品透過橋接成分適合哪些膚質 ──
        r4 = s.run("""
            MATCH (i_kb:Ingredient)-[:SUITABLE_FOR_CONCERN]->(sc:SkinConcern)
            WHERE elementId(i_kb) IN $kb_ids
              AND toLower(i_kb.name_en) IN $name_en_lowers
            MATCH (p:Product)-[:CONTAINS]->(i_a:Ingredient)
            WHERE elementId(p) IN $product_ids
              AND toLower(i_a.name_en) = toLower(i_kb.name_en)
            RETURN
                elementId(p)              AS product_id,
                p.name                    AS product_name,
                p.category                AS category,
                collect(DISTINCT sc.name) AS suits
        """, kb_ids=kb_ids, name_en_lowers=name_en_lowers, product_ids=product_ids)

        for row in r4:
            result['product_suits'].append(dict(row))

        # ── 邊7：類型B Ingredient CONFLICTS_WITH ──
        r7 = s.run("""
            MATCH (i1:Ingredient)-[:CONFLICTS_WITH]->(i2:Ingredient)
            WHERE elementId(i1) IN $ids OR elementId(i2) IN $ids
            RETURN
                i1.name_zh AS ingredient_a,
                i2.name_zh AS ingredient_b
        """, ids=kb_ids)

        for row in r7:
            result['conflicts'].append(dict(row))

        # ── 邊6：Regulation → Category ──
        r6 = s.run("""
            MATCH (p:Product) WHERE elementId(p) IN $ids
          AND coalesce(p.recommendable, true) = true
            WITH collect(DISTINCT p.category) AS cats
            MATCH (reg:Regulation)-[:REGULATES_CATEGORY]->(cat:Category)
            WHERE cat.name IN cats
            RETURN
                reg.text    AS text,
                reg.type    AS type,
                cat.name    AS applies_to
            LIMIT 5
        """, ids=product_ids)

        for row in r6:
            result['regulations'].append(dict(row))

    return result


def format_graph_result(result: dict) -> str:
    lines = []

    if result.get('ingredients'):
        names = list({
            i['ingredient_name_zh']
            for i in result['ingredients']
            if i.get('ingredient_name_zh')
        })
        lines.append(f"【邊1 含有成分】{' / '.join(names[:8])}")

    if result.get('risks'):
        for r in result['risks'][:3]:
            if r.get('ingredient') and r.get('description'):
                lines.append(f"【邊2+3 成分風險】{r['ingredient']} → {r['description'][:50]}")

    if result.get('product_suits'):
        for ps in result['product_suits'][:3]:
            lines.append(f"【邊4 商品適合】{ps['product_name'][:25]} → {ps['suits'][:3]}")

    if result.get('suitable_concerns'):
        for sc in result['suitable_concerns'][:3]:
            if sc.get('ingredient'):
                lines.append(f"【邊5 成分適合】{sc['ingredient']} → {sc['suitable_concerns'][:3]}")

    if result.get('regulations'):
        for reg in result['regulations'][:2]:
            lines.append(f"【邊6 法規規範】[{reg['type']}] {str(reg['text'])[:50]}...")

    if result.get('conflicts'):
        for c in result['conflicts'][:3]:
            lines.append(f"【邊7 成分衝突】{c['ingredient_a']} × {c['ingredient_b']}")

    if result.get('warnings'):
        lines.append(f"【警告】{'；'.join(result['warnings'][:3])}")
    else:
        lines.append("【警告】無特殊禁忌")

    return '\n'.join(lines)


if __name__ == '__main__':
    print("=== Neo4j Graph Search 測試（橋接版）===\n")

    driver = get_driver()
    with driver.session() as s:
        # 找含維他命C（Ascorbic Acid）的商品
        samples = s.run("""
            MATCH (p:Product)-[:CONTAINS]->(i:Ingredient)
            WHERE toLower(i.name_en) IN ['ascorbic acid', 'vitamin c', 'niacinamide']
            WITH p, collect(i.name_en) AS ings
            RETURN elementId(p) AS id, p.name AS name, p.category AS category, ings
            LIMIT 3
        """).data()

    if not samples:
        print("找不到含維他命C的商品")
    else:
        product_ids = [s['id'] for s in samples]
        print("測試商品：")
        for s in samples:
            print(f"  - {s['name']} [{s['category']}] 成分：{s['ings']}")

        print("\n--- 敏感肌測試（應有維他命C警告）---")
        result = graph_search(product_ids, user_skin_type='sensitive')
        print(format_graph_result(result))

        print("\n--- 乾肌測試 ---")
        result2 = graph_search(product_ids, user_skin_type='dry')
        print(format_graph_result(result2))

        print("\n--- 油肌測試 ---")
        result3 = graph_search(product_ids, user_skin_type='oily')
        print(format_graph_result(result3))

    if _driver:
        _driver.close()
