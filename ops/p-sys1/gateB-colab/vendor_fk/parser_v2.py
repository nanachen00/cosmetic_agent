#!/usr/bin/env python3
"""parser_v2 — overlay on the ORIGINAL Parser (payload/kev_slot_parser.py, untouched). Gate A compares v1 (original) vs v2.
Fixes: Chinese numerals / 1.5k / 萬, ranges, unit-attached numbers (ml/g/歲...) excluded from budget candidates,
'A酸' (\\b never matches between Latin and CJK), explicit-avoid ingredient hints (negation -> excluded list), role hints.
Output keeps every v1 field and adds: budget_candidates, budget_upper, budget_target, ingredients, avoid_hint, fixes."""
from __future__ import annotations
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload"))
import kev_slot_parser as P

ING_ZH = {"hyaluronic_acid": "玻尿酸", "ceramide": "神經醯胺", "niacinamide": "菸鹼醯胺", "retinol": "A酸",
          "salicylic_acid": "水楊酸", "aha": "果酸"}
ING_PAT = {"hyaluronic_acid": r"玻尿酸|透明質酸|hyaluronic\s*acid", "ceramide": r"神經醯胺|ceramide",
           "niacinamide": r"菸鹼醯胺|煙鹼醯胺|煙醯胺|niacinamide", "retinol": r"視黃醇|A\s*酸|retinol",
           "salicylic_acid": r"水楊酸|salicylic\s*acid", "aha": r"果酸|AHA"}
D = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
U = {"十": 10, "百": 100, "千": 1000, "萬": 10000}
CN = r"[一二兩三四五六七八九十零〇]*[萬千百][一二兩三四五六七八九十零〇百千]*"
NOT_BUDGET_AFTER = r"\s*(?:ml|mL|ML|毫升|g\b|公克|克|mg|歲|天|週|周|個|支|瓶|次|%|％|入|片|盒|年|月|倍|公升|L\b|秒|分鐘|小時)"


def cn_to_int(s: str):
    total, section, num = 0, 0, None
    last_unit = None
    for ch in s:
        if ch in D:
            num = D[ch]
        elif ch in U:
            u = U[ch]
            if u == 10000:
                section = (section + (num or 0)) or 1
                total += section * u; section = 0
            else:
                section += (num if num is not None else 1) * u
            num = None; last_unit = u
        else:
            return None
    if num is not None:                              # trailing digit: 一千五 -> 1500, 兩萬五 -> 25000
        section += num * (last_unit // 10 if last_unit and last_unit > 1 else 1)
    return total + section or None


UP = re.compile(r"(?:以內|以下|不超過|不要超過|別超過|最多|上限|最高|之內|為限|封頂)")
TG = re.compile(r"(?:左右|大約|約|差不多|上下|抓|落在)")
CAND = re.compile(rf"(?P<pre>預算(?:是|為|大約|約)?|NT\$?|\$)?\s*(?P<num>{CN}|\d+(?:\.\d+)?\s*[kK萬]?)\s*(?P<unit>元|塊錢|塊|圓|台幣|NT)?")


def _val(tok: str):
    tok = tok.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kK萬]?)", tok)
    if m:
        v = float(m.group(1)) * {"": 1, "k": 1000, "K": 1000, "萬": 10000}[m.group(2)]
        return int(v)
    return cn_to_int(tok)


def budget_candidates(t: str):
    out = []
    for m in CAND.finditer(t):
        tok, unit, pre = m.group("num"), m.group("unit"), m.group("pre")
        if not tok.strip():
            continue
        has_scale = bool(re.search(r"[kK萬]$", tok.strip())) or bool(re.fullmatch(CN, tok.strip()))
        after_range = bool(out) and bool(re.search(r"(?:到|至|~|～|-|－)\s*$", t[:m.start()])) and bool(re.fullmatch(r"\d{3,6}", tok.strip()))
        if not (unit or pre or has_scale or after_range):
            continue
        if re.match(NOT_BUDGET_AFTER, t[m.end():], re.I):
            continue
        v = _val(tok)
        if v is None or v < 50 or v > 500000:       # 50 TWD floor: avoids '3 瓶', '10 天'
            continue
        out.append({"span": m.group(0).strip(), "value": v, "start": m.start(), "end": m.end(), "role": None})
    # ranges: A (到|~|至|-) B
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if re.fullmatch(r"\s*(?:到|至|~|～|-|－|–|—)\s*", t[a["end"]:b["start"]]):
            a["role"], b["role"] = "range_lo", "range_hi"
    for c in out:
        if c["role"]:
            continue
        before, after = t[max(0, c["start"] - 8):c["start"]], t[c["end"]:c["end"] + 8]
        if re.search(r"(?:不超過|不要超過|別超過|最高|最多|上限)\s*(?:預算)?\s*$", before) or UP.match(after.lstrip()):
            c["role"] = "upper"
        elif TG.match(after.lstrip()) or re.search(r"(?:大約|約|差不多|抓)\s*$", before):
            c["role"] = "target"
    return out


def avoid_hints(t: str):
    hits = []
    for key, pat in ING_PAT.items():
        for m in re.finditer(pat, t, re.I):
            before = t[max(0, m.start() - 8):m.start()]
            after = t[m.end():m.end() + 8]
            if re.search(r"(?:不要|不想|避開|避免|不能|不可以|別|不含|無|拒絕)(?:含|用|使用|有|任何|再用)?\s*$", before) or \
               re.search(r"^\s*(?:先?不要|不可以|過敏|會過敏)", after):
                hits.append(key); break
    return hits


def ingredients_fixed(t: str):
    return [k for k, pat in ING_PAT.items() if re.search(pat, t, re.I)]


def parse2(text: str) -> dict:
    p = P.parse(text).to_dict()
    t = text.strip()
    cands = budget_candidates(t)
    fixes = []
    ing = ingredients_fixed(t)
    if set(ing) - set(p.get("ingredients") or []):
        fixes.append("ingredient_cjk_boundary")
    upper = target = None
    for c in cands:
        if c["role"] in ("upper", "range_hi") and upper is None: upper = c["value"]
        if c["role"] == "target" and target is None: target = c["value"]
    if upper is None: upper = p.get("budget_twd_max")
    if target is None: target = p.get("budget_twd_target")
    if len(cands) and (p.get("budget_twd_max") is None and p.get("budget_twd_target") is None): fixes.append("budget_candidates_added")
    p.update(ingredients=ing, budget_candidates=cands, budget_upper=upper, budget_target=target,
             avoid_hint=avoid_hints(t), fixes=fixes)
    return p


if __name__ == "__main__":
    for s in sys.argv[1:] or ["預算八百，想找防曬", "預算500到1000的面霜", "50ml 的乳液 800 元", "不要A酸，預算最高1200元"]:
        r = parse2(s); print(s, "->", r["budget_upper"], r["budget_target"], [(c["value"], c["role"]) for c in r["budget_candidates"]], r["ingredients"], r["avoid_hint"])
