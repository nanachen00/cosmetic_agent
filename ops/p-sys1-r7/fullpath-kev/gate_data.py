#!/usr/bin/env python3
"""gate_data — seeded synthetic labelled sentences for Gate A (no production data, no real users).
Case = {id, text, group, safety:bool, intents:[...], concern:str|None, budget:{value,role}|None, avoid:[ing keys], other_numbers:bool, emergency:bool}
intents ⊂ {find_product, price_comparison, knowledge, record, chitchat, unclear}.  Split cal/test by hash(text) so thresholds are
tuned on cal and reported on test only.  Gold is synthetic; it checks wiring/robustness, not real-user accuracy (stated in the report).
"""
from __future__ import annotations
import hashlib, json, random, sys

SKIN = ["油性肌", "乾性肌", "敏感肌", "混合肌", "油肌", "乾肌"]
CAT = ["乳液", "面霜", "洗面乳", "防曬", "精華", "化妝水", "面膜"]
CONCERN = {"保濕": ["保濕", "補水"], "抗痘": ["抗痘", "改善痘痘"], "控油": ["控油"], "美白": ["美白"], "舒緩": ["舒緩", "鎮靜"],
           "抗皺": ["抗皺"], "毛孔": ["縮小毛孔", "改善毛孔"]}
ING = {"hyaluronic_acid": "玻尿酸", "ceramide": "神經醯胺", "niacinamide": "菸鹼醯胺", "retinol": "A酸", "salicylic_acid": "水楊酸", "aha": "果酸"}
CN_AMT = {300: "三百", 500: "五百", 800: "八百", 1000: "一千", 1200: "一千二", 1500: "一千五", 2000: "兩千", 3000: "三千"}


def amt_variants(v, role, rng):
    arabic = rng.choice([f"{v}元", f"{v} 元", f"NT${v}", f"{v}塊"])
    cn = CN_AMT.get(v)
    k = f"{v/1000:g}k" if v >= 1000 else None
    form = rng.choice([x for x in (arabic, arabic, cn and f"{cn}元", k) if x])
    if role == "upper": return rng.choice([f"預算{form}以內", f"{form}以內", f"預算不超過{form}", f"最多{form}", f"預算最高{form}"]).replace("以內元", "以內")
    if role == "target": return rng.choice([f"預算{form}左右", f"大約{form}", f"{form}左右"])
    return f"預算{form}"


def mk(rng, n_per=24):
    C = []
    add = lambda **k: C.append(dict(dict(safety=False, intents=[], concern=None, budget=None, avoid=[], other_numbers=False, emergency=False), **k))
    for i in range(n_per * 2):                                         # find_product
        sk, cat = rng.choice(SKIN), rng.choice(CAT); con = rng.choice(list(CONCERN)) if rng.random() < .6 else None
        v = rng.choice(list(CN_AMT)); role = rng.choice(["upper", "upper", "target"])
        txt = f"我是{sk}，想找{cat}" + (f"，主要想{rng.choice(CONCERN[con])}" if con else "") + "，" + amt_variants(v, role, rng)
        add(text=txt, group="find", intents=["find_product"], concern=con, budget={"value": v, "role": role})
    for i in range(n_per):                                             # find, no budget
        cat = rng.choice(CAT); con = rng.choice(list(CONCERN))
        add(text=rng.choice([f"推薦一款{rng.choice(CONCERN[con])}的{cat}", f"有沒有{rng.choice(CONCERN[con])}的{cat}可以推薦", f"想買{cat}，{rng.choice(CONCERN[con])}為主"]),
            group="find", intents=["find_product"], concern=con)
    for i in range(n_per):                                             # price comparison
        a, b = rng.sample(CAT, 2)
        add(text=rng.choice([f"這兩款{a}哪一款比較划算", f"{a}A和{a}B價格差多少，哪個CP值高", f"幫我比較兩瓶{a}的價格"]), group="price", intents=["price_comparison"])
    for i in range(n_per):                                             # knowledge
        k = rng.choice(list(ING))
        add(text=rng.choice([f"{ING[k]}是什麼，有什麼功效", f"{ING[k]}的作用原理是什麼", f"為什麼{ING[k]}可以幫助保養", f"{ING[k]}適合什麼膚質"]), group="knowledge", intents=["knowledge"])
    for i in range(n_per // 2):
        add(text=rng.choice(["為什麼我的臉最近一直脫皮", "T字部位出油是什麼原因", "冬天臉為什麼特別乾"]), group="knowledge", intents=["knowledge"])
    for i in range(n_per):                                             # record
        add(text=rng.choice(["幫我記錄今天下巴冒了兩顆痘", "記錄一下今天臉頰有點紅", "今天額頭脫皮，幫我記起來", "幫我記錄今天的膚況，T字有點油"]), group="record", intents=["record"])
    for t in ["你好", "謝謝你", "哈囉", "今天天氣真好", "你是誰", "晚安", "辛苦了", "謝謝，沒有其他問題了"]:
        add(text=t, group="chitchat", intents=["chitchat"])
    for t in ["我想", "那個呢", "幫我弄一下", "這個可以嗎", "我想要那個", "還有嗎"]:
        add(text=t, group="unclear", intents=["unclear"])
    for i in range(n_per):                                             # safety (alone)
        a, b = rng.sample(list(ING), 2)
        add(text=rng.choice([f"孕婦可以用{ING[a]}嗎", f"{ING[a]}和{ING[b]}可以一起用嗎", f"{ING[a]}會不會刺激過敏", f"哺乳期間能不能擦{ING[a]}", f"敏感肌用{ING[a]}安全嗎"]),
            group="safety", safety=True, intents=["knowledge"])
    for t in ["擦完之後臉腫起來，喘不過氣", "用完嘴唇腫脹呼吸困難", "全身起紅疹還覺得喘"]:
        add(text=t, group="emergency", safety=True, emergency=True, intents=["knowledge"])
    for i in range(n_per):                                             # compound
        sk, cat = rng.choice(SKIN), rng.choice(CAT); k = rng.choice(list(ING)); v = rng.choice(list(CN_AMT))
        kind = i % 5
        if kind == 0: add(text=f"{ING[k]}是什麼？順便推薦{cat}，{amt_variants(v,'upper',rng)}", group="compound", intents=["knowledge", "find_product"], budget={"value": v, "role": "upper"})
        elif kind == 1: add(text=f"孕婦可以用{ING[k]}嗎？另外推薦一款{cat}", group="compound", safety=True, intents=["knowledge", "find_product"])
        elif kind == 2: add(text=f"記錄今天臉頰脫皮，順便推薦{cat}，{amt_variants(v,'upper',rng)}", group="compound", intents=["record", "find_product"], budget={"value": v, "role": "upper"})
        elif kind == 3: add(text=f"幫我記錄今天臉頰紅，然後告訴我{ING[k]}的作用", group="compound", intents=["record", "knowledge"])
        else: add(text=f"{sk}想找{cat}，{amt_variants(v,'upper',rng)}，{ING[k]}可以一起用嗎", group="compound", safety=True, intents=["find_product", "knowledge"], budget={"value": v, "role": "upper"})
    for i in range(n_per):                                             # traps
        cat = rng.choice(CAT); k = rng.choice(list(ING)); v = rng.choice([500, 800, 1000, 1200])
        kind = i % 6
        if kind == 0: add(text=f"想找{rng.choice([30,50,100])}ml 的{cat}，{amt_variants(v,'upper',rng)}", group="trap_number", intents=["find_product"], budget={"value": v, "role": "upper"}, other_numbers=True)
        elif kind == 1: add(text=f"我{rng.choice([22,28,35,41])}歲，{amt_variants(v,'upper',rng)}，推薦{cat}", group="trap_number", intents=["find_product"], budget={"value": v, "role": "upper"}, other_numbers=True)
        elif kind == 2: add(text=f"我沒有過敏，只是想找{cat}，{amt_variants(v,'upper',rng)}", group="trap_negation", intents=["find_product"], budget={"value": v, "role": "upper"})
        elif kind == 3: add(text=f"想找{cat}，不要{ING[k]}，{amt_variants(v,'upper',rng)}", group="trap_avoid", intents=["find_product"], budget={"value": v, "role": "upper"}, avoid=[k])
        elif kind == 4: add(text=f"不是要找商品，只是想問{ING[k]}的原理", group="trap_negation", intents=["knowledge"])
        else: add(text=f"想買{cat}，避免使用{ING[k]}，{amt_variants(v,'target',rng)}", group="trap_avoid", intents=["find_product"], budget={"value": v, "role": "target"}, avoid=[k])
    return C


def regression_set():
    """The 20 walkthrough sentences (text-only ones) with hand-labelled gold."""
    R = [
        ("S1", "我是油性肌，想找一瓶控油的洗面乳，預算 800 以內", dict(intents=["find_product"], concern="控油", budget={"value": 800, "role": "upper"})),
        ("S2", "菸鹼醯胺和水楊酸有什麼差別", dict(intents=["knowledge"])),
        ("S3", "敏感肌可以一起用水楊酸和A酸嗎", dict(intents=["knowledge"], safety=True)),
        ("S4", "擦完之後臉腫起來，喘不過氣", dict(intents=["knowledge"], safety=True, emergency=True)),
        ("S5", "幫我記錄今天下巴冒了兩顆痘", dict(intents=["record"])),
        ("S6", "記錄今天臉頰脫皮，順便推薦保濕乳液，預算一千以內", dict(intents=["record", "find_product"], concern="保濕", budget={"value": 1000, "role": "upper"})),
        ("S7", "A酸是什麼？有沒有推薦的A酸精華，預算1500元以內", dict(intents=["knowledge", "find_product"], budget={"value": 1500, "role": "upper"})),
        ("S8", "孕婦可以用水楊酸嗎？另外推薦一款洗面乳", dict(intents=["knowledge", "find_product"], safety=True)),
        ("S9", "我想", dict(intents=["unclear"])),
        ("S10", "我30歲，油性肌，預算800元以內", dict(intents=["find_product"], budget={"value": 800, "role": "upper"}, other_numbers=True)),
        ("S12", "50ml 的乳液 800 元", dict(intents=["find_product"], budget={"value": 800, "role": "any"}, other_numbers=True)),
        ("S13", "預算500到1000的面霜", dict(intents=["find_product"], budget={"value": 1000, "role": "upper"})),
        ("S14", "你好", dict(intents=["chitchat"])),
        ("S16", "最近臉一直脫皮，是為什麼，還有要用什麼保養品", dict(intents=["knowledge", "find_product"])),
        ("S17", "我想找無香料的精華，不要A酸，預算最高1200元", dict(intents=["find_product"], budget={"value": 1200, "role": "upper"}, avoid=["retinol"])),
        ("S19", "幫我記錄今天臉頰紅，然後告訴我煙醯胺的作用", dict(intents=["record", "knowledge"])),
        ("S20", "預算八百，想找防曬", dict(intents=["find_product"], budget={"value": 800, "role": "upper"})),
    ]
    out = []
    for id_, t, g in R:
        out.append(dict(dict(safety=False, intents=[], concern=None, budget=None, avoid=[], other_numbers=False, emergency=False), id=id_, text=t, group="regression", **g))
    return out


def build(seed=7, n_per=24):
    rng = random.Random(seed)
    C = mk(rng, n_per) + regression_set()
    seen, out = set(), []
    for c in C:
        if c["text"] in seen: continue
        seen.add(c["text"]); out.append(c)
    for i, c in enumerate(out):
        c.setdefault("id", f"c{i:04d}")
        c["split"] = "cal" if int(hashlib.md5(c["text"].encode()).hexdigest(), 16) % 2 == 0 else "test"
        if c["group"] == "regression": c["split"] = "test"
    return out


if __name__ == "__main__":
    d = build(); print(len(d), "cases", {g: sum(1 for c in d if c["group"] == g) for g in sorted({c["group"] for c in d})})
    if len(sys.argv) > 1: json.dump(d, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
