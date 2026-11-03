"""Kev -> Agent dispatch overlay.  SHADOW ONLY.  Installed by shadowpatch.py as agents/root/kev_dispatch.py.

Replaces the LLM intent parse (`parse_intent`) at the Root-orchestrator call sites with:
    Kev (:8230 /v1/systemone)  ->  route  ->  IntentParseResult  ->  existing agent dispatch
and NEVER falls back to the LLM:  Kev error / timeout / low confidence  ->  UNCLEAR  ->  the orchestrator's own
clarification question.   Budget / ingredients (numbers & free tokens) come from a deterministic regex parser,
because Kev only answers `choice` / `noul` questions.

Inert unless ALL hold:  marker file `.kev-shadow-overlay` next to the `agents/` dir whose recorded pkg_root equals
this file's package root (shadowpatch writes it only into a shadow copy)  AND  not located under a production root
AND  control arm != "off".   Arms (re-read from the control file on every call -> switch without restart):
    off          original LLM parse_intent (the patch is a pass-through)
    kev_route    Kev route; skin/category/budget/ingredients from the regex parser; concern left empty
    kev_slots    Kev route + Kev `choice` slots (skin_type, product_category, concern, texture); budget/ingredients from the parser
Control file: $KEV_DISPATCH_CONTROL (json) e.g. {"arm":"kev_route","conf_min":0.5,"safety_guard":0.5,"clarify_guard":0.5,
 "slot_conf_min":0.5,"timeout_s":8,"skip_pain_gain":false,"trace":"/path/trace.jsonl","kev_url":"http://127.0.0.1:8230"}
"""
from __future__ import annotations
import hashlib, json, os, re, threading, time, urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve()
_PKG = _HERE.parents[2]
_PROD = [Path(p).resolve() for p in ("/var/lib/jenkins/tomod/cosmetic-agent", "/var/lib/jenkins/tomod/cosmetic-agent-stable")]
_ROUTE_DESC = {
    "product": "商品推薦/搜尋/預算篩選", "knowledge": "成分與保養知識/比較", "skin_tracking": "膚況紀錄/追蹤",
    "conversation": "一般聊天/結束語", "safety": "過敏、刺激、禁忌、懷孕或交互作用", "clarify": "資訊不足需釐清"}
_QUESTIONS_BASE = {
    "route": {"type": "choice", "instructions": "依使用者真正要完成的主要任務選一個路由；資訊不足時選 clarify。", "criteria": _ROUTE_DESC},
    "needs_clarification": {"type": "noul", "instructions": "是否必須先追問才能安全決定主要任務？", "criteria": {"true": "需要追問", "false": "可直接分派"}},
    "safety_sensitive": {"type": "noul", "instructions": "是否涉及過敏、刺激、禁忌、懷孕、交互作用或其他需要優先安全處理的內容？", "criteria": {"true": "安全敏感", "false": "沒有明顯安全風險"}},
}
_SLOT_Q = {
    "skin_type": {"type": "choice", "instructions": "使用者自述的膚質；沒提到選 unknown。",
                  "criteria": {"oily": "油性", "dry": "乾性", "combination": "混合性", "sensitive": "敏感性", "normal": "中性/正常", "unknown": "沒提到"}},
    "product_category": {"type": "choice", "instructions": "使用者想找的商品品類；沒提到選 不明。",
                         "criteria": {"洗面乳": "洗面乳", "化妝水": "化妝水", "面膜": "面膜", "精華液": "精華液", "乳液": "乳液", "乳霜": "乳霜/面霜",
                                      "保濕棉片": "保濕棉片", "防曬乳": "防曬", "不明": "沒提到"}},
    "concern": {"type": "choice", "instructions": "使用者最主要的肌膚訴求；沒提到選 不明。",
                "criteria": {"保濕": "保濕", "緊緻": "緊緻", "抗皺": "抗皺", "亮白": "亮白", "透亮": "透亮", "逆齡": "逆齡", "舒緩": "舒緩", "舒敏": "舒敏",
                             "抗痘": "抗痘", "控油": "控油", "毛孔": "毛孔", "美白": "美白", "淡斑": "淡斑", "不明": "沒提到"}},
    "texture": {"type": "choice", "instructions": "使用者偏好的質地；沒提到選 不明。",
                "criteria": {"清爽": "清爽", "滋潤": "滋潤", "凝膠": "凝膠", "乳霜": "乳霜質地", "油類": "油類", "不明": "沒提到"}},
}
_CAT_RX = [("洗面乳", r"洗面乳|潔面"), ("化妝水", r"化妝水|爽膚水"), ("面膜", r"面膜"), ("精華液", r"精華"),
           ("乳霜", r"乳霜|面霜|保濕霜"), ("乳液", r"乳液"), ("防曬乳", r"防曬")]

_lock = threading.Lock()
_ctl_cache = {"mtime": None, "data": {}}
_parser = None
_tls = threading.local()


def _enabled_here() -> bool:
    if any(_PKG == p or p in _PKG.parents for p in _PROD): return False
    mk = _PKG / ".kev-shadow-overlay"
    try: return Path(json.loads(mk.read_text())["pkg_root"]).resolve() == _PKG
    except Exception: return False


def _ctl() -> dict:
    path = os.environ.get("KEV_DISPATCH_CONTROL")
    if not path:
        try: path = json.loads((_PKG / ".kev-shadow-overlay").read_text()).get("control")   # set by shadowpatch at apply time
        except Exception: path = None
    base = {"arm": os.environ.get("KEV_DISPATCH_ARM", "off"), "conf_min": 0.5, "safety_guard": 0.5, "clarify_guard": 0.5,
            "slot_conf_min": 0.5, "timeout_s": 8.0, "skip_pain_gain": False, "trace": os.environ.get("KEV_DISPATCH_TRACE", ""),
            "kev_url": os.environ.get("KEV_URL", "http://127.0.0.1:8230")}
    if path:
        try:
            m = os.stat(path).st_mtime_ns
            if m != _ctl_cache["mtime"]:
                _ctl_cache.update(mtime=m, data=json.loads(Path(path).read_text(encoding="utf-8")))
            base.update(_ctl_cache["data"])
        except Exception: pass
    if not _enabled_here(): base["arm"] = "off"
    if not re.fullmatch(r"http://(127\.0\.0\.1|localhost):823\d/?", str(base["kev_url"])):
        base["arm"] = "off"                                  # only Kev ports 8230 (real) / 8231-8239 (tests), never anything else
    return base


def _trace(ctl: dict, rec: dict):
    p = ctl.get("trace")
    if not p: return
    try:
        with _lock, open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass


def _load_parser():
    global _parser
    if _parser is None:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("kev_slot_parser", str(_HERE.with_name("kev_slot_parser.py")))
        mod = importlib.util.module_from_spec(spec); sys.modules["kev_slot_parser"] = mod; spec.loader.exec_module(mod); _parser = mod
    return _parser


def _kev_call(ctl: dict, text: str, slots: bool) -> tuple[dict, float]:
    q = dict(_QUESTIONS_BASE)
    if slots: q.update(_SLOT_Q)
    body = json.dumps({"state": text, "model": "kev-latest", "questions": q}).encode()
    req = urllib.request.Request(ctl["kev_url"].rstrip("/") + "/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=float(ctl["timeout_s"])) as r:
        out = json.loads(r.read().decode())
    return out, (time.perf_counter() - t) * 1000


def _decide(ans: dict, ctl: dict) -> dict:
    r = ans["answers"]["route"]; probs = {k: float(v) for k, v in r["probabilities"].items()}
    route, conf = r["choice"], float(r.get("confidence", max(probs.values())))
    clar = float(ans["answers"]["needs_clarification"]["noul"]); saf = float(ans["answers"]["safety_sensitive"]["noul"])
    why = "kev"
    if route not in _ROUTE_DESC: route, why = "clarify", "bad_route"
    elif saf >= ctl["safety_guard"] and route not in ("safety",): route, why = "safety", "safety_guard"
    elif route not in ("clarify", "safety") and clar >= ctl["clarify_guard"]: route, why = "clarify", "clarify_guard"
    elif route not in ("clarify",) and conf < ctl["conf_min"]: route, why = "clarify", "low_confidence"
    return {"route": route, "kev_choice": r["choice"], "confidence": conf, "clarify_p": clar, "safety_p": saf, "why": why}


def _slots_from_parser(text: str) -> dict:
    p = _load_parser().parse(text)
    cat = next((c for c, rx in _CAT_RX if re.search(rx, text)), None)
    budget = p.budget_twd_max or p.budget_twd_target
    return {"skin_type": p.skin_type, "product_category": cat, "budget_range": str(budget) if budget else None,
            "ingredients": list(p.ingredients or [])}


def _slots_from_kev(ans: dict, ctl: dict) -> dict:
    out = {}
    for k in ("skin_type", "product_category", "concern", "texture"):
        a = ans["answers"].get(k) or {}
        ch, cf = a.get("choice"), float(a.get("confidence", 0) or 0)
        if ch and ch not in ("unknown", "不明") and cf >= ctl["slot_conf_min"]: out[k] = ch
    return out


def _build_intent(text: str, dec: dict, slots: dict, kev_slots: dict):
    from schemas.intent import (IntentParseResult, IntentType, SkinType, ProductCategory, SkinConcern, TexturePreference,
                                calculate_ambiguity_score)
    route = dec["route"]
    itype = {"product": IntentType.FIND_PRODUCT, "knowledge": IntentType.KNOWLEDGE_QUERY,
             "safety": IntentType.FIND_PRODUCT}.get(route, IntentType.UNCLEAR)      # skin_tracking/conversation/clarify -> UNCLEAR
    def enum(cls, v, default):
        try: return cls(v)
        except Exception: return default
    skin = enum(SkinType, kev_slots.get("skin_type") or slots.get("skin_type"), SkinType.UNKNOWN)
    cat = enum(ProductCategory, kev_slots.get("product_category") or slots.get("product_category"), ProductCategory.UNKNOWN)
    tex = enum(TexturePreference, kev_slots.get("texture"), TexturePreference.UNKNOWN)
    concern = [kev_slots["concern"]] if kev_slots.get("concern") in {c.value for c in SkinConcern} else []
    use_slots = route in ("product", "safety", "knowledge")
    res = IntentParseResult(
        intent_type=itype, skin_type=skin if use_slots else SkinType.UNKNOWN, concern=concern if use_slots else [],
        product_category=cat if use_slots else ProductCategory.UNKNOWN, texture_preference=tex if use_slots else TexturePreference.UNKNOWN,
        budget_range=slots.get("budget_range") if route == "product" else None, query_expansion=list(slots.get("ingredients") or []),
        raw_query=text)
    return res.model_copy(update={"ambiguity_score": calculate_ambiguity_score(res)})


def parse_intent(_orch=None, _fallback=None, **kw):
    """Drop-in for tools.agent.intent_parser.parse_intent (same keyword arguments)."""
    ctl = _ctl()
    if ctl["arm"] == "off" or _orch is None:
        _tls.last = None
        return _fallback(**kw)
    text = kw["user_query"]
    t0 = time.perf_counter(); rec = {"ts": time.time(), "arm": ctl["arm"], "q_sha": hashlib.sha256(text.encode()).hexdigest(),
                                      "chars": len(text), "thread": threading.get_ident()}
    try:
        ans, kev_ms = _kev_call(ctl, text, slots=(ctl["arm"] == "kev_slots"))
        dec = _decide(ans, ctl)
        rec.update(kev_ms=round(kev_ms, 2), kev_server_ms=ans.get("latency_ms"), **{k: dec[k] for k in dec})
        t1 = time.perf_counter()
        slots = _slots_from_parser(text) if dec["route"] in ("product", "safety", "knowledge") else {}
        kslots = _slots_from_kev(ans, ctl) if ctl["arm"] == "kev_slots" else {}
        rec["parser_ms"] = round((time.perf_counter() - t1) * 1000, 3)
        intent = _build_intent(text, dec, slots, kslots)
        rec.update(slots_kev=kslots, slots_parser={k: v for k, v in slots.items() if v})
    except Exception as e:                                  # NO LLM FALLBACK: ask the user instead
        dec = {"route": "clarify", "why": "kev_error:" + type(e).__name__}
        rec.update(route="clarify", why=dec["why"], error=str(e)[:200])
        from schemas.intent import IntentParseResult, IntentType
        intent = IntentParseResult(intent_type=IntentType.UNCLEAR, raw_query=text, ambiguity_score=1.0)
    rec["intent_total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    _tls.last = {"text": text, "route": dec["route"]}
    _trace(ctl, rec)
    return intent


def early_dispatch(orch, user_input: str, intent):
    """skin_tracking has no IntentType: hand over to the Skin-Tracking agent directly."""
    last = getattr(_tls, "last", None)
    if not last or last["text"] != user_input or last["route"] != "skin_tracking": return None
    _tls.last = None
    orch.state.active_flow = "skin_record"
    return orch._handle_skin_tracking_input(user_input)


def force_safety(orch, intent) -> bool:
    last = getattr(_tls, "last", None)
    return bool(last and last["route"] == "safety" and last["text"] == (intent.raw_query or ""))


def skip_pain_gain() -> bool:
    c = _ctl(); return c["arm"] != "off" and bool(c.get("skip_pain_gain"))
