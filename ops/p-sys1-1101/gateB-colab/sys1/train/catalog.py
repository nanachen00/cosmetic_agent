#!/usr/bin/env python3
"""catalog — P+Sys1 微調候選共用的「題組 ↔ 頭」對應、標註推導、排除集合。stdlib only（不 import torch）。

頭（所有微調候選都一樣，handoff §4 規則 4）：
  sentence 頭：safety(1, sigmoid) · emergency(1, sigmoid) · intent(6, 各自 sigmoid) · concern(14, softmax)
  pair 頭    ：role(3, softmax；輸入=句子+金額片段) · avoid(1, sigmoid；輸入=句子+成分名)
題組名對應（gateb.pk_bundle / kevlib.production_bundle 會送的題）：
  safety, emergency, i_<label>, concern, role_<i>, avoid_<ing>
題目內容必須與凍結的題庫（kevlib / gateb.EMERGENCY_Q）逐字相同；不同 → 拒答（頭沒學過那個問法，不能亂答）。
"""
from __future__ import annotations
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__)); SYS1 = os.path.dirname(HERE); ROOT = os.path.dirname(SYS1)
for p in (os.path.join(ROOT, "vendor_fk"), ROOT, SYS1):
    if p not in sys.path: sys.path.insert(0, p)
import kevlib as K          # noqa: E402  (frozen; read-only)
import parser_v2 as P2      # noqa: E402  (frozen; read-only)

LABELS = list(K.INTENT_LABELS)
CONCERN_LABELS = list(K.CONCERNS) + [K.NO_CONCERN]
ROLE_LABELS = list(K.ROLE_CRITERIA)                     # upper, target, other
ING_ZH = dict(P2.ING_ZH)
HEADS = {"safety": 1, "emergency": 1, "intent": len(LABELS), "concern": len(CONCERN_LABELS), "role": len(ROLE_LABELS), "avoid": 1}
SENT_HEADS = ("safety", "emergency", "intent", "concern")
PAIR_HEADS = ("role", "avoid")


def emergency_q():
    """gateb.EMERGENCY_Q（凍結檔）；import gateb 只為了拿這一題，確保逐字一致。"""
    import gateb as GB
    return GB.EMERGENCY_Q


def norm(t: str) -> str:
    """與 sys1/holdout_v2.norm 相同（排除檢查用）。"""
    return re.sub(r"[\s\W_]+", "", t).lower()


# ----------------------------------------------------------------------------- question set → plan
_ROLE_RE = re.compile(r"^句中的「(.+?)」（約 (\d+)）代表什麼？$", re.S)
_AVOID_RE = re.compile(r"^使用者是否明確表示不要、要避免或不能使用「(.+?)」？$", re.S)


def _same(q, ref):
    return isinstance(q, dict) and q.get("type") == ref["type"] and q.get("instructions") == ref["instructions"] and dict(q.get("criteria") or {}) == dict(ref["criteria"])


def plan_questions(qs: dict):
    """回傳 (plan, errors)。plan: list of (name, head, arg)；head ∈ safety/emergency/intent/concern/role/avoid。
    任何一題不認得或內容與題庫不符 → errors 非空，伺服器整個請求回 422（不部分作答）。"""
    plan, errs = [], []
    if not isinstance(qs, dict) or not qs: return [], ["questions 必須是非空 object"]
    em = emergency_q()
    for name, q in qs.items():
        if name == "safety":
            (plan.append((name, "safety", None)) if _same(q, K.SAFETY_Q) else errs.append(f"{name}: 內容與凍結題庫 kevlib.SAFETY_Q 不符"))
        elif name == "emergency":
            (plan.append((name, "emergency", None)) if _same(q, em) else errs.append(f"{name}: 內容與凍結題庫 gateb.EMERGENCY_Q 不符"))
        elif name.startswith("i_") and name[2:] in K.INTENT_LABELS:
            lab = name[2:]
            (plan.append((name, "intent", LABELS.index(lab))) if _same(q, K.intent_noul_q(lab)) else errs.append(f"{name}: 內容與 kevlib.intent_noul_q({lab!r}) 不符"))
        elif name == "concern":
            (plan.append((name, "concern", None)) if _same(q, K.concern_q()) else errs.append(f"{name}: 內容與 kevlib.concern_q() 不符（選項必須是 {len(CONCERN_LABELS)} 個訴求）"))
        elif re.fullmatch(r"role_\d+", name):
            m = _ROLE_RE.match((q or {}).get("instructions", "")) if isinstance(q, dict) else None
            if not m: errs.append(f"{name}: instructions 不是 kevlib.role_q 格式"); continue
            ref = K.role_q(m.group(1), int(m.group(2)))
            (plan.append((name, "role", m.group(1))) if _same(q, ref) else errs.append(f"{name}: 內容與 kevlib.role_q 不符"))
        elif name.startswith("avoid_"):
            m = _AVOID_RE.match((q or {}).get("instructions", "")) if isinstance(q, dict) else None
            if not m: errs.append(f"{name}: instructions 不是 kevlib.avoid_q 格式"); continue
            ref = K.avoid_q(m.group(1))
            (plan.append((name, "avoid", m.group(1))) if _same(q, ref) else errs.append(f"{name}: 內容與 kevlib.avoid_q 不符"))
        else:
            errs.append(f"{name}: 不支援的題目（這個 Sys1 只有 {', '.join(HEADS)} 頭；不亂答）")
    return plan, errs


# ----------------------------------------------------------------------------- labels (gate_data case format → head targets)
def sentence_targets(c: dict) -> dict:
    con = c.get("concern")
    return {"safety": float(bool(c.get("safety"))), "emergency": float(bool(c.get("emergency"))),
            "intent": [float(l in (c.get("intents") or [])) for l in LABELS],
            "concern": CONCERN_LABELS.index(con) if con in CONCERN_LABELS else CONCERN_LABELS.index(K.NO_CONCERN)}


def pair_examples(c: dict, p2: dict | None = None):
    """和 kevlib.full_bundle 同一套候選：Parser 找到的前 3 個金額片段（role）與前 3 個成分（avoid）。
    role 標註：與 gold 預算同值且 gold role ∈ {upper,target} → 該 role；Parser 判為 range_lo 或 gold role=any → 不出題（decide 不讀）；其餘 → other。"""
    p2 = p2 or P2.parse2(c["text"]); out = []
    gb = c.get("budget")
    for cand in p2.get("budget_candidates", [])[:3]:
        if cand.get("role") == "range_lo": continue
        if gb and gb.get("role") == "any": continue
        if gb and cand["value"] == gb["value"] and gb["role"] in ("upper", "target"): lab = gb["role"]
        elif gb and cand["value"] == gb["value"]: continue
        else: lab = "other"
        out.append(("role", cand["span"], ROLE_LABELS.index(lab)))
    for key in p2.get("ingredients", [])[:3]:
        out.append(("avoid", ING_ZH.get(key, key), float(key in (c.get("avoid") or []))))
    return out


# ----------------------------------------------------------------------------- evaluation sentences that training must never contain
def eval_texts(extra_runs=()):
    """回傳 {來源: set(norm text)}。任何一個讀不到 → 直接失敗（排除檢查不能靜默變弱）。"""
    import gate_data as D, extra_safety as XS, holdout as HO
    S = {}
    gd = D.build(7, 24)
    S["gate_data_cal"] = {norm(c["text"]) for c in gd if c["split"] == "cal"}
    S["gate_data_test"] = {norm(c["text"]) for c in gd if c["split"] == "test"}
    S["extra_safety"] = {norm(c["text"]) for c in XS.cases()}
    S["router_gold"] = {norm(c["text"]) for c in HO.cases()}
    v2 = os.path.join(SYS1, "holdout_v2", "router_gold_v2.json")
    S["router_gold_v2"] = {norm(c["query"]) for c in json.load(open(v2, encoding="utf-8"))["cases"]} if os.path.exists(v2) else set()
    for r in extra_runs:                                       # gateb run dirs built with other --seed/--n-per
        S[f"run:{r}"] = {norm(c["text"]) for c in json.load(open(os.path.join(r, "cases.json"), encoding="utf-8"))}
    return S


def cal_cases():
    """temperature scaling 用：gate_data 的 cal 切分（P+Sys1 的門檻也只在這裡調）。"""
    import gate_data as D
    return [c for c in D.build(7, 24) if c["split"] == "cal"]
