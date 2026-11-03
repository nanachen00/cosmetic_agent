#!/usr/bin/env python3
"""gemma_arms — Gemma 臂的 schema、prompt、few-shot 與「輸出 → 統一判定」的對應。Stdlib only.

三種 schema（每種都分 prompt_only／json_schema 兩種解碼，兩者送出的 messages 完全相同，只差 response_format）：
  ext    = 已知 IntentParseResult 欄位 + Gate 金標需要、但 production schema 沒有的欄位（多意圖、安全、急症、預算角色、避開成分）
  min    = 只有金標要評分的欄位（輸出最短，代表「純 LLM、短輸出」的最佳延遲）
  legacy = tests/fixture 的 IntentParseResult 原樣（單一 intent_type、budget_range 字串），Legacy LLM 路徑的結構替身：
           無法表達安全、記錄、閒聊、避開成分 → 這些項目會結構性失分（這正是要量的）
  *_fs   = 同 schema，system prompt 多附 few-shot 範例（例句刻意不用 gate_data 的模板）

為了公平：意圖說明直接取自 kevlib.INTENT_LABELS、安全定義取自 kevlib.SAFETY_Q、訴求清單取自 kevlib.CONCERNS，
金額慣例（區間取上限、容量／年齡不是預算）與 Parser 的規則一致——Kev 從 Parser 得到這些慣例，Gemma 從 prompt 得到。
prompt 文字放在 prompts/*.txt，可直接替換成 production 原文（legacy_system.txt 目前是替身，不是 production prompt）。
"""
from __future__ import annotations
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor_fk"))
import kevlib as K                                   # noqa: E402
import parser_v2 as P2                               # noqa: E402

LABELS = list(K.INTENT_LABELS)
ING_KEYS = list(P2.ING_ZH)
ING_ZH = P2.ING_ZH
SKIN = ["oily", "dry", "combination", "sensitive", "normal", "unknown"]
CATEGORY = ["洗面乳", "化妝水", "面膜", "精華液", "乳液", "乳霜", "保濕棉片", "防曬乳", "acne_patch", "pore_strip", "exfoliant",
            "lip_care", "spot_care", "eye_care", "makeup_remover", "不明"]
TEXTURE = ["清爽", "滋潤", "凝膠", "乳霜", "油類", "不明"]
LEGACY_INTENT = ["symptom_solving", "find_product", "recommendation", "price_comparison", "knowledge_query", "unclear"]
LEGACY_TO_GATE = {"find_product": "find_product", "recommendation": "find_product", "price_comparison": "price_comparison",
                  "knowledge_query": "knowledge", "symptom_solving": "knowledge", "unclear": "unclear"}
CONCERN_SYNONYM = {"補水": "保濕", "鎮靜": "舒緩", "改善痘痘": "抗痘", "痘痘": "抗痘", "縮小毛孔": "毛孔", "改善毛孔": "毛孔"}

SCHEMAS = ("ext", "min", "legacy", "prod")   # prod = production SYSTEM_PROMPT + INTENT_JSON_SCHEMA verbatim (prod_replay.py)
PROD_ARMS = ["prod_json", "prod_prompt"]
MODES = ("json", "prompt")
DEFAULT_ARMS = ["ext_json", "ext_prompt", "min_json", "min_prompt", "legacy_json", "legacy_prompt", "min_fs_json", "min_fs_prompt"]


def arm_parts(arm: str):
    """'min_fs_json' -> ('min', True, 'json')"""
    parts = arm.split("_")
    if len(parts) not in (2, 3) or parts[0] not in SCHEMAS or parts[-1] not in MODES or (len(parts) == 3 and parts[1] != "fs"):
        raise SystemExit(f"[gateb] unknown arm {arm!r}; valid: <ext|min|legacy>[_fs]_<json|prompt>")
    return parts[0], len(parts) == 3, parts[-1]


# ----------------------------------------------------------------------------- JSON schemas
def _enum_arr(values, max_items):
    return {"type": "array", "items": {"type": "string", "enum": list(values)}, "maxItems": max_items}


def _pr():
    import prod_replay
    return prod_replay


def schema_for(kind: str) -> dict:
    if kind == "prod":
        return _pr().JSON_SCHEMA
    gate = {
        "intents": dict(_enum_arr(LABELS, 3), minItems=1),
        "safety_flag": {"type": "boolean"},
        "emergency_flag": {"type": "boolean"},
    }
    tail = {
        "budget_value": {"type": ["integer", "null"]},
        "budget_role": {"type": "string", "enum": ["upper", "target", "none"]},
        "avoid_ingredients": _enum_arr(ING_KEYS, 3),
    }
    if kind == "min":
        props = dict(gate, concern=_enum_arr(K.CONCERNS, 3), **tail)
    elif kind == "ext":
        props = dict(gate, skin_type={"type": "string", "enum": SKIN}, concern=_enum_arr(K.CONCERNS, 3),
                     product_category={"type": "string", "enum": CATEGORY}, texture_preference={"type": "string", "enum": TEXTURE},
                     **tail, brand_preference={"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 3},
                     urgency_flag={"type": "boolean"}, query_expansion={"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 5})
    elif kind == "legacy":
        props = {"intent_type": {"type": "string", "enum": LEGACY_INTENT}, "skin_type": {"type": "string", "enum": SKIN},
                 "concern": {"type": "array", "items": {"type": "string", "maxLength": 10}, "maxItems": 3},
                 "product_category": {"type": "string", "enum": CATEGORY}, "texture_preference": {"type": "string", "enum": TEXTURE},
                 "budget_range": {"type": ["string", "null"], "maxLength": 20},
                 "brand_preference": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 3},
                 "urgency_flag": {"type": "boolean"},
                 "query_expansion": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 5}}
    else:
        raise ValueError(kind)
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ----------------------------------------------------------------------------- prompts
def _read(name):
    return open(os.path.join(HERE, "prompts", name), encoding="utf-8").read().strip()


def _fill(tpl: str) -> str:
    return (tpl.replace("{INTENT_LINES}", "\n".join(f"  {k}＝{v}" for k, v in K.INTENT_LABELS.items()))
               .replace("{SAFETY_DEF}", K.SAFETY_Q["instructions"].removeprefix("這句話是否").rstrip("？"))
               .replace("{CONCERNS}", "、".join(K.CONCERNS))
               .replace("{ING_LINES}", "、".join(f"{k}（{v}）" for k, v in ING_ZH.items()))
               .replace("{SKIN}", "、".join(SKIN)).replace("{CATEGORY}", "、".join(CATEGORY)).replace("{TEXTURE}", "、".join(TEXTURE)))


def system_prompt(kind: str, fewshot: bool) -> str:
    if kind == "prod":
        if fewshot: raise SystemExit("[gateb] prod arms have no few-shot variant (the production prompt already carries its examples)")
        return _pr().SYSTEM_PROMPT
    s = _fill(_read(f"{kind}_system.txt"))
    if fewshot:
        s += "\n\n" + _read(f"{kind}_fewshot.txt")
    return s


def messages(arm: str, text: str) -> list:
    kind, fs, _ = arm_parts(arm)
    user = _pr().user_content(text) if kind == "prod" else text          # production wraps the turn with routing context
    return [{"role": "system", "content": system_prompt(kind, fs)}, {"role": "user", "content": user}]


def request_body(arm: str, text: str, max_tokens: int, seed: int = 0) -> dict:
    kind, _, mode = arm_parts(arm)
    if kind == "prod": max_tokens = _pr().MAX_OUTPUT_TOKENS              # production: max_output_tokens=400
    b = {"model": "gemma", "messages": messages(arm, text), "temperature": 0.0, "top_k": 1, "top_p": 1.0, "seed": seed,
         "max_tokens": max_tokens, "stream": False, "chat_template_kwargs": {"enable_thinking": False},
         # v2: with response_format the first run still got a free-text "Thinking Process" before the JSON (q8_0: 101/101 rows,
         # 350-512 tokens, many truncated). Budget 0 = end any thinking block immediately; reasoning_effort none = thinking off.
         "reasoning_budget_tokens": 0, "reasoning_effort": "none"}
    if mode == "json":
        b["response_format"] = {"type": "json_schema", "json_schema": {"name": f"intent_{kind}", "strict": True, "schema": schema_for(kind)}}
    return b


def prompt_signature() -> str:
    import hashlib
    blob = json.dumps({a: [system_prompt(*arm_parts(a)[:2]), schema_for(arm_parts(a)[0])] for a in DEFAULT_ARMS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------- parse + map to the unified decision
def extract_json(raw: str):
    """-> (obj | None, error | None). Tolerates ``` fences and leading/trailing prose (prompt_only arms)."""
    if raw is None:
        return None, "empty"
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        o = json.loads(s)
        return (o, None) if isinstance(o, dict) else (None, "not_object")
    except Exception:
        pass
    dec = json.JSONDecoder(); i = s.find("{")
    while i != -1:                                   # first position where a complete JSON object decodes
        try:
            o, _ = dec.raw_decode(s, i)
            if isinstance(o, dict): return o, None
        except Exception:
            pass
        i = s.find("{", i + 1)
    return None, ("truncated_or_invalid" if "{" in s else "no_json")


def _as_list(v):
    if v is None: return []
    if isinstance(v, (list, tuple)): return list(v)
    return [v]


def _ing_key(v):
    v = str(v).strip()
    if v in ING_ZH: return v
    for k, pat in P2.ING_PAT.items():
        if re.fullmatch(f"(?:{pat})", v, re.I) or re.search(pat, v, re.I): return k
    return None


def _budget_value(v):
    if v is None or isinstance(v, bool): return None
    if isinstance(v, (int, float)): return int(v) if v > 0 else None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "無", "不明"): return None
    c = P2.budget_candidates(s if re.search(r"元|塊|NT|\$|預算|[kK萬]", s) else s + "元")
    if not c: return None
    hi = [x for x in c if x["role"] == "range_hi"]
    return (hi[0] if hi else c[0])["value"]


def to_decision(kind: str, obj) -> tuple[dict, dict]:
    """Gemma JSON -> (decision in gateA.errors() format, notes). Unknown/invalid values are dropped and counted in notes."""
    notes = {"dropped": [], "normalised": []}
    out = {"safety": False, "intents": [], "budget": None, "avoid": [], "concern": None, "emergency": False}
    if not isinstance(obj, dict):
        return out, notes
    if kind == "legacy":
        it = str(obj.get("intent_type", "")).strip()
        if it in LEGACY_TO_GATE: out["intents"] = [LEGACY_TO_GATE[it]]
        elif it: notes["dropped"].append(f"intent_type={it}")
        v = _budget_value(obj.get("budget_range"))
        if v: out["budget"] = {"value": v, "role": "upper"}       # production field is documented as「預算上限」
    else:
        ints = []
        for x in _as_list(obj.get("intents")):
            x = str(x).strip().lower()
            if x in LABELS and x not in ints: ints.append(x)
            elif x: notes["dropped"].append(f"intent={x}")
        out["intents"] = sorted(ints)
        out["safety"] = obj.get("safety_flag") is True or str(obj.get("safety_flag")).lower() == "true"
        out["emergency"] = obj.get("emergency_flag") is True or str(obj.get("emergency_flag")).lower() == "true"
        out["safety"] = out["safety"] or out["emergency"]          # same rule as P+K v2: an emergency report IS a safety case
        v = _budget_value(obj.get("budget_value")); role = str(obj.get("budget_role", "")).strip().lower()
        if v and role in ("upper", "target"): out["budget"] = {"value": v, "role": role}
        elif v and role not in ("none",):
            out["budget"] = {"value": v, "role": "upper"}; notes["normalised"].append(f"budget_role={role or '∅'}→upper")
        elif v:
            notes["dropped"].append(f"budget_value={v} with role=none")
        av = []
        for x in _as_list(obj.get("avoid_ingredients")):
            k = _ing_key(x)
            if k and k not in av: av.append(k)
            elif not k and str(x).strip(): notes["dropped"].append(f"avoid={x}")
            if k and str(x).strip() != k: notes["normalised"].append(f"avoid {x}→{k}")
        out["avoid"] = sorted(av)
    con = None
    for x in _as_list(obj.get("concern")):
        x = str(x).strip()
        y = x if x in K.CONCERNS else CONCERN_SYNONYM.get(x)
        if y:
            if y != x: notes["normalised"].append(f"concern {x}→{y}")
            con = y; break
        elif x and x not in ("不明", K.NO_CONCERN, "無"): notes["dropped"].append(f"concern={x}")
    out["concern"] = con
    return out, notes


def validate(kind: str, obj) -> list:
    """Cheap schema check (types / enums / required) -> list of violations; [] = valid."""
    sc = schema_for(kind); v = []
    if not isinstance(obj, dict): return ["not_object"]
    for k, p in sc["properties"].items():
        if k not in obj: v.append(f"missing:{k}"); continue
        x = obj[k]; t = p.get("type") or [a.get("type") for a in p.get("anyOf", [])] or None
        if t is None: continue
        ok_t = {"string": lambda z: isinstance(z, str), "boolean": lambda z: isinstance(z, bool), "array": lambda z: isinstance(z, list),
                "integer": lambda z: isinstance(z, int) and not isinstance(z, bool), "null": lambda z: z is None,
                "number": lambda z: isinstance(z, (int, float)) and not isinstance(z, bool), "object": lambda z: isinstance(z, dict)}
        ts = t if isinstance(t, list) else [t]
        if not any(ok_t[tt](x) for tt in ts): v.append(f"type:{k}"); continue
        if "enum" in p and x not in p["enum"]: v.append(f"enum:{k}")
        if isinstance(x, list) and "items" in p and "enum" in p["items"] and any(i not in p["items"]["enum"] for i in x): v.append(f"enum:{k}[]")
    extra = set(obj) - set(sc["properties"])
    if extra: v.append("extra:" + ",".join(sorted(extra)))
    return v


if __name__ == "__main__":
    for a in sys.argv[1:] or ["min_json"]:
        k, fs, m = arm_parts(a)
        print(f"===== {a} (schema={k} fewshot={fs} mode={m})\n{system_prompt(k, fs)}\n----- schema\n{json.dumps(schema_for(k), ensure_ascii=False)}\n")
