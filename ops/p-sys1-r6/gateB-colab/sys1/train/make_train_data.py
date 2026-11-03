#!/usr/bin/env python3
"""make_train_data — P+Sys1 微調候選的 typed 訓練集（所有候選共用同一份；handoff §4 規則 3、4）。

來源（只有這三種；provenance.json 逐一記錄）：
  template  本檔的模板／規則生成（seeded；用詞刻意避開 gate_data / extra_safety 的模板句型）
  real      --real FILE.jsonl：負責人提供的去識別化真實問法（每列需有 gate_data 格式的標註；檔案層級需 consent 欄位）
  synth     --synth FILE.jsonl：開源『非中國』模型產生的合成句；每列必須有 generator 欄位，命中 policy.DENY_FAMILIES 即整批拒絕

排除檢查（失敗即 exit 3，不寫任何輸出）：訓練句正規化後（holdout_v2.norm）不得出現在
  gate_data cal/test、extra_safety、router_gold、router_gold_v2（若存在）、--exclude-run 給的 gateb run 的 cases.json。
另外列出近似重複（字元 bigram Jaccard ≥ --near 門檻）供人工看；預設只報告，--strict-near 時也會失敗。

  python3 sys1/train/make_train_data.py --out sys1/train/data/v1 [--seed 11] [--per 120] [--real f.jsonl --real-consent "…"] [--synth f.jsonl]
輸出：train.jsonl、dev.jsonl（以 hash 切 9:1）、provenance.json、exclusion_report.json、SHA256SUMS
"""
from __future__ import annotations
import argparse, hashlib, json, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import catalog as C   # noqa: E402
sys.path.insert(0, C.SYS1)
import policy          # noqa: E402

GEN_VERSION = "tpl-v1"
SKIN = ["油性肌膚", "乾性肌膚", "敏弱肌", "混合偏油", "混合偏乾", "痘痘肌", "一般肌", "超乾肌"]
CAT = {"乳液": "乳液", "乳霜": "乳霜", "潔顏": "潔顏露", "防曬": "防曬乳", "精華": "精華液", "化妝水": "化妝水", "面膜": "面膜", "卸妝": "卸妝油", "眼霜": "眼霜", "凝膠": "保濕凝膠"}
CON = {"保濕": ["保水", "鎖水", "解決乾燥"], "緊緻": ["拉提緊緻", "讓臉緊實"], "抗皺": ["淡化細紋", "對付皺紋"], "亮白": ["提亮膚色", "讓膚色亮一點"],
       "舒緩": ["退紅", "安撫泛紅"], "舒敏": ["降低敏感", "穩定敏感"], "抗痘": ["壓痘", "減少冒痘"], "控油": ["抑制出油", "改善油光"],
       "毛孔": ["收毛孔", "讓毛孔不明顯"], "美白": ["淡化暗沉", "美白"], "淡斑": ["淡化斑點", "處理曬斑"], "透亮": ["透亮感", "讓氣色透亮"], "逆齡": ["抗老", "凍齡"]}
ING = {"hyaluronic_acid": ["玻尿酸", "透明質酸"], "ceramide": ["神經醯胺"], "niacinamide": ["菸鹼醯胺", "煙醯胺"], "retinol": ["A醇", "視黃醇", "A酸"],
       "salicylic_acid": ["水楊酸", "BHA水楊酸"], "aha": ["果酸", "甘醇酸果酸"]}
OTHER_ING = ["維他命C", "杏仁酸", "胜肽", "積雪草", "角鯊烷", "傳明酸", "壬二酸", "茶樹精油", "熊果素", "B5"]
BRAND = ["甲牌", "乙牌", "丙牌", "丁牌", "某開架品牌", "專櫃那款"]


def amount(v, role, rng):
    if role == "upper":
        return rng.choice([f"錢控制在{v}元內", f"{v}塊以下就好", f"不要超過{v}元", f"上限抓{v}元", f"預算是{v}元以內", f"最多花{v}塊", f"NT${v}以下"])
    if role == "target":
        return rng.choice([f"{v}元上下", f"大概花{v}塊", f"差不多{v}元", f"預算約{v}元", f"{v}塊左右的價位"])
    return f"{v}元"


def build(seed, per):
    rng = random.Random(seed); out = []
    def add(text, **k):
        d = dict(safety=False, emergency=False, intents=[], concern=None, budget=None, avoid=[], other_numbers=False)
        d.update(k); d["text"] = text; d["source"] = "template"; out.append(d)
    V = [300, 450, 600, 700, 900, 1100, 1300, 1600, 1800, 2500, 3500, 4000]
    for _ in range(per * 2):                                   # find_product + budget
        sk, cat = rng.choice(SKIN), rng.choice(list(CAT)); con = rng.choice(list(CON)) if rng.random() < .65 else None
        v = rng.choice(V); role = rng.choice(["upper", "upper", "target"]); cp = f"{rng.choice(CON[con])}" if con else ""
        t = rng.choice([f"{sk}，想挑一款{CAT[cat]}" + (f"可以{cp}" if con else "") + f"，{amount(v, role, rng)}",
                        f"幫我挑{CAT[cat]}，{amount(v, role, rng)}" + (f"，希望能{cp}" if con else ""),
                        f"{amount(v, role, rng)}，{sk}適合哪款{CAT[cat]}" + (f"？想要{cp}" if con else "？"),
                        f"有{CAT[cat]}的口袋名單嗎，{amount(v, role, rng)}" + (f"，重點是{cp}" if con else "")])
        add(t, group="find", intents=["find_product"], concern=con, budget={"value": v, "role": role})
    for _ in range(per):                                       # find, no budget
        cat = rng.choice(list(CAT)); con = rng.choice(list(CON)); cp = rng.choice(CON[con])
        add(rng.choice([f"求一款能{cp}的{CAT[cat]}", f"哪款{CAT[cat]}比較能{cp}", f"想換一瓶{CAT[cat]}，最好能{cp}", f"{rng.choice(SKIN)}用的{CAT[cat]}有好物嗎？要能{cp}"]),
            group="find", intents=["find_product"], concern=con)
    for _ in range(per // 2):
        cat = rng.choice(list(CAT))
        add(rng.choice([f"{rng.choice(SKIN)}的{CAT[cat]}有什麼選擇", f"想買新的{CAT[cat]}，給我幾個選項", f"最近缺一瓶{CAT[cat]}，有推的嗎"]), group="find", intents=["find_product"])
    for _ in range(per):                                       # price comparison
        cat = CAT[rng.choice(list(CAT))]; a, b = rng.sample(BRAND, 2)
        add(rng.choice([f"{a}跟{b}的{cat}誰比較便宜", f"{cat}買大容量是不是比較省", f"{a}的{cat}一毫升換算起來貴不貴", f"同樣是{cat}，{a}和{b}哪個價錢比較實在", f"網購跟藥妝店買{cat}哪邊比較便宜"]),
            group="price", intents=["price_comparison"])
    for _ in range(per):                                       # knowledge (ingredients incl. non-parser ones)
        name = rng.choice([x for v in ING.values() for x in v] + OTHER_ING)
        add(rng.choice([f"{name}主要是做什麼用的", f"{name}對皮膚有什麼幫助", f"想了解{name}的原理", f"{name}要早上擦還是晚上擦", f"{name}的濃度怎麼看"]), group="knowledge", intents=["knowledge"])
    for _ in range(per // 2):
        add(rng.choice(["鼻頭粉刺一直清不乾淨是什麼原因", "為什麼換季皮膚就變得很敏感", "化妝水和精華的順序是什麼", "防曬係數SPF跟PA差在哪",
                        "睡眠不足會影響膚況嗎", "洗臉水溫要多熱比較好", "磨砂膏跟酵素粉哪種去角質方式比較溫和", "為什麼下午臉就開始泛油光"]), group="knowledge", intents=["knowledge"])
    REG = ["額頭", "兩頰", "下巴", "鼻翼", "嘴角", "眼下", "T字", "鼻頭", "髮際線"]
    SYM = ["冒了兩顆痘", "有點脫屑", "泛紅", "比較油", "很乾很緊繃", "暗沉", "有點刺癢", "的痘痘消了", "粉刺變多", "狀況穩定"]
    for _ in range(per):                                       # record
        r_, s_ = rng.choice(REG), rng.choice(SYM)
        add(rng.choice([f"幫我記下來，今天{r_}{s_}", f"記一筆：{r_}{s_}", f"今天{r_}{s_}，存進紀錄", f"幫我寫進保養日誌，{r_}{s_}",
                        f"日誌加一筆，今天{r_}{s_}", f"今天的膚況：{r_}{s_}，幫我記錄"]), group="record", intents=["record"])
    for g in ["嗨嗨", "早～", "午安呀", "晚上好", "哈囉哈囉", "安安"]:
        for t in ["", "，在嗎", "，今天也請多指教", "，最近好嗎"]:
            add(g + t, group="chitchat", intents=["chitchat"])
    for t in ["感謝幫忙", "辛苦你啦", "先這樣，下次聊", "你是機器人嗎", "今天好累喔", "哈哈好喔", "收到，謝啦", "你平常都在做什麼", "好喔掰", "你好可愛", "謝謝你的建議，很有用", "我今天心情不太好"]:
        add(t, group="chitchat", intents=["chitchat"])
    for t in ["然後呢", "那個", "嗯…", "這樣可以嗎", "那另一個", "幫我看看", "我要這個", "剩下的呢", "所以呢", "就那個啊", "上次那個", "跟剛剛一樣", "換一個",
              "那這個呢", "可以嗎", "這個咧", "嗯嗯那", "不是這個", "就是說", "對啊然後", "那要怎樣"]:
        add(t, group="unclear", intents=["unclear"])
    for _ in range(per):                                       # safety questions
        a, b = rng.sample(list(ING), 2); na, nb = rng.choice(ING[a]), rng.choice(ING[b]); oth = rng.choice(OTHER_ING)
        add(rng.choice([f"懷孕中擦{na}會有風險嗎", f"{na}跟{nb}同一晚擦會不會出問題", f"餵奶期間{na}可以繼續用嗎", f"我對{oth}過敏，那{na}還能用嗎",
                        f"{na}會不會讓皮膚太敏感", f"剛做完雷射可以馬上擦{na}嗎", f"吃A酸口服藥的時候能擦{na}嗎", f"{na}和{oth}疊擦安全嗎", f"小朋友可以用含{na}的產品嗎"]),
            group="safety", safety=True, intents=["knowledge"])
    USE = ["擦完", "塗了", "敷完", "用了", "換了", "噴了", "卸完妝用了"]
    PROD = ["新買的乳霜", "面膜", "那瓶精華", "防曬", "果酸", "眼霜", "朋友給的乳液", "藥膏", "新的化妝水"]
    RE1 = ["臉整個腫起來", "嘴唇發麻腫脹", "喉嚨變得很緊", "眼皮腫到睜不開", "整片起水泡", "全身冒紅疹", "脖子腫起來"]
    RE2 = ["有點吸不到氣", "開始喘", "頭很暈想吐", "心跳很快", "講話不太清楚", "越來越嚴重", "胸口很悶"]
    for _ in range(per):                                       # emergency reports
        add(f"{rng.choice(USE)}{rng.choice(PROD)}之後{rng.choice(RE1)}，{rng.choice(RE2)}", group="emergency", safety=True, emergency=True, intents=["knowledge"])
    for i in range(per):                                       # compound
        cat = CAT[rng.choice(list(CAT))]; k = rng.choice(list(ING)); nk = rng.choice(ING[k]); v = rng.choice(V); kind = i % 5
        if kind == 0: add(f"順便問一下{nk}有什麼作用？也幫我挑{cat}，{amount(v, 'upper', rng)}", group="compound", intents=["knowledge", "find_product"], budget={"value": v, "role": "upper"})
        elif kind == 1: add(f"備孕期間能擦{nk}嗎？然後想找一瓶{cat}", group="compound", safety=True, intents=["knowledge", "find_product"])
        elif kind == 2: add(f"今天鼻子很乾先幫我記下來，另外想買{cat}，{amount(v, 'upper', rng)}", group="compound", intents=["record", "find_product"], budget={"value": v, "role": "upper"})
        elif kind == 3: add(f"記一下今天臉有點癢，順便說明{nk}是什麼", group="compound", intents=["record", "knowledge"])
        else: add(f"想挑{cat}，{amount(v, 'upper', rng)}，另外{rng.choice(ING[rng.choice([x for x in ING if x != 'retinol'])])}跟A醇能同時用嗎", group="compound", safety=True, intents=["find_product", "knowledge"], budget={"value": v, "role": "upper"})
    for i in range(per):                                       # traps
        cat = CAT[rng.choice(list(CAT))]; k = rng.choice(list(ING)); nk = rng.choice(ING[k]); v = rng.choice([450, 600, 900, 1100]); kind = i % 7
        if kind == 0: add(f"要{rng.choice([15, 40, 120, 200])}ml裝的{cat}，{amount(v, 'upper', rng)}", group="trap_number", intents=["find_product"], budget={"value": v, "role": "upper"}, other_numbers=True)
        elif kind == 1: add(f"今年{rng.choice([19, 26, 33, 47])}歲，想找{cat}，{amount(v, 'upper', rng)}", group="trap_number", intents=["find_product"], budget={"value": v, "role": "upper"}, other_numbers=True)
        elif kind == 2: add(f"之前買過{v + 700}元那瓶太貴了，這次想找{cat}，{amount(v, 'upper', rng)}", group="trap_number", intents=["find_product"], budget={"value": v, "role": "upper"}, other_numbers=True)
        elif kind == 3: add(f"皮膚沒什麼過敏問題，單純想找{cat}，{amount(v, 'upper', rng)}", group="trap_negation", intents=["find_product"], budget={"value": v, "role": "upper"})
        elif kind == 4: add(f"{cat}請避開{nk}，{amount(v, 'upper', rng)}", group="trap_avoid", intents=["find_product"], budget={"value": v, "role": "upper"}, avoid=[k])
        elif kind == 5: add(f"先不買東西，只想了解{nk}怎麼作用", group="trap_negation", intents=["knowledge"])
        else: add(f"有沒有不含{nk}的{cat}，{amount(v, 'target', rng)}", group="trap_avoid", intents=["find_product"], budget={"value": v, "role": "target"}, avoid=[k])
    for _ in range(per // 2):                                  # ingredient mentioned but NOT avoided (avoid negatives)
        cat = CAT[rng.choice(list(CAT))]; k = rng.choice(list(ING)); nk = rng.choice(ING[k])
        t, ints = rng.choice([(f"想找含{nk}的{cat}", ["find_product"]), (f"有添加{nk}的{cat}推薦嗎", ["find_product"]), (f"{cat}裡面有{nk}會比較好嗎", ["knowledge"])])
        add(t, group="ing_positive", intents=ints)
    seen, dd = set(), []
    for d in out:
        n = C.norm(d["text"])
        if n in seen: continue
        seen.add(n); dd.append(d)
    return dd


def load_jsonl(p, source):
    rows = []
    for i, line in enumerate(open(p, encoding="utf-8")):
        if not line.strip(): continue
        r = json.loads(line)
        if "text" not in r: sys.exit(f"[data] {p}:{i + 1} 缺 text")
        d = dict(safety=False, emergency=False, intents=[], concern=None, budget=None, avoid=[], other_numbers=False, group=source)
        d.update(r); d["source"] = source; rows.append(d)
    return rows


def bigrams(s): return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); ap.add_argument("--seed", type=int, default=11); ap.add_argument("--per", type=int, default=120)
    ap.add_argument("--real"); ap.add_argument("--real-consent", help="真實問法的同意／去識別化紀錄（必填於 --real）"); ap.add_argument("--synth", action="append", default=[])
    ap.add_argument("--exclude-run", action="append", default=[]); ap.add_argument("--near", type=float, default=0.8); ap.add_argument("--strict-near", action="store_true")
    ap.add_argument("--inject", help="測試用：把這個 JSONL 當 template 來源混入（用來證明排除檢查會擋）")
    a = ap.parse_args()
    rows = build(a.seed, a.per); prov = {"generator_version": GEN_VERSION, "seed": a.seed, "per": a.per, "sources": [{"source": "template", "generator": f"make_train_data.py {GEN_VERSION} (rule/template, no LLM)", "n": len(rows)}]}
    if a.inject: inj = load_jsonl(a.inject, "template"); rows += inj; prov["sources"].append({"source": "template(inject-test)", "file": a.inject, "n": len(inj)})
    if a.real:
        if not a.real_consent: sys.exit("[data] --real 需要 --real-consent（去識別化與同意紀錄）")
        r = load_jsonl(a.real, "real"); rows += r
        prov["sources"].append({"source": "real", "file": a.real, "sha256": hashlib.sha256(open(a.real, "rb").read()).hexdigest(), "n": len(r), "consent": a.real_consent})
    for sp in a.synth:
        r = load_jsonl(sp, "synth"); gens = sorted({str(x.get("generator") or "") for x in r})
        if "" in gens: sys.exit(f"[data] {sp}：有列沒有 generator 欄位（合成句必須記錄產生模型）")
        bad = [g for g in gens if any(d in g.lower() for d in policy.DENY_FAMILIES)]
        if bad: print(f"[data] REFUSED {sp}：generator 命中中國模型家族 {bad}（handoff §1；不得用中國模型產標註）"); return 4
        rows += r; prov["sources"].append({"source": "synth", "file": sp, "sha256": hashlib.sha256(open(sp, "rb").read()).hexdigest(), "n": len(r), "generators": gens})
    # ---- exclusion
    S = C.eval_texts(a.exclude_run); hits = []
    for d in rows:
        n = C.norm(d["text"])
        for name, s in S.items():
            if n in s: hits.append({"text": d["text"], "source": d["source"], "in": name})
    allev = {n: name for name, s in S.items() for n in s}; evb = [(n, bigrams(n), nm) for n, nm in allev.items()]
    near = []
    for d in rows:
        n = C.norm(d["text"]); b = bigrams(n); best = (0.0, None, None)
        for en, eb, nm in evb:
            j = len(b & eb) / len(b | eb)
            if j > best[0]: best = (j, en, nm)
        if best[0] >= a.near and n not in allev: near.append({"text": d["text"], "jaccard": round(best[0], 3), "closest": best[1], "in": best[2]})
    rep = {"eval_sets": {k: len(v) for k, v in S.items()}, "exact_hits": hits, "near_threshold": a.near, "near_dups": near, "n_train_rows": len(rows)}
    os.makedirs(a.out, exist_ok=True); json.dump(rep, open(os.path.join(a.out, "exclusion_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if hits:
        print(f"[data] EXCLUSION FAILED：{len(hits)} 句與評估集重複（見 {a.out}/exclusion_report.json）"); [print("   ", h) for h in hits[:10]]
        for f in ("train.jsonl", "dev.jsonl", "provenance.json", "SHA256SUMS"):
            if os.path.exists(os.path.join(a.out, f)): os.remove(os.path.join(a.out, f))
        return 3
    if near and a.strict_near: print(f"[data] NEAR-DUP FAILED：{len(near)} 句（--strict-near）"); return 3
    # ---- write
    for d in rows: d["id"] = "t" + hashlib.md5(d["text"].encode()).hexdigest()[:10]; d["split"] = "dev" if int(d["id"][1:], 16) % 10 == 0 else "train"
    tr = [d for d in rows if d["split"] == "train"]; dv = [d for d in rows if d["split"] == "dev"]
    for name, part in (("train.jsonl", tr), ("dev.jsonl", dv)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
            for d in part: f.write(json.dumps(d, ensure_ascii=False) + "\n")
    grp = {}
    for d in rows: grp[d["group"]] = grp.get(d["group"], 0) + 1
    prov.update(n_train=len(tr), n_dev=len(dv), by_group=grp, exclusion={"exact_hits": 0, "near_dups_reported": len(near), "eval_sets": rep["eval_sets"]},
                rule="不得含 cal/test/extra_safety/router_gold/router_gold_v2 句子（正規化後）；合成句不得由中國模型產生")
    json.dump(prov, open(os.path.join(a.out, "provenance.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "SHA256SUMS"), "w") as f:
        for name in ("train.jsonl", "dev.jsonl", "provenance.json"): f.write(f"{hashlib.sha256(open(os.path.join(a.out, name), 'rb').read()).hexdigest()}  {name}\n")
    print(f"[data] OK train={len(tr)} dev={len(dv)} groups={grp} near-dups(報告)={len(near)} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
