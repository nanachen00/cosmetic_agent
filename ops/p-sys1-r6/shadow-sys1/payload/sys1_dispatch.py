"""P+Sys1 -> Agent dispatch overlay.  SHADOW ONLY.  Installed by shadowpatch.py as agents/root/sys1_dispatch.py.
(Copy-and-rewrite of fullpath-kev/payload/kev_dispatch.py; the original file is untouched.)

Replaces the LLM `parse_intent` at the Root-orchestrator call sites with the Gate B decision path, so S2 (hold-out) and S3 (shadow)
evaluate the SAME policy:
    parse2(text) -> pk_bundle -> Sys1 POST /v1/systemone -> decide_v2 (frozen thresholds sys1/frozen/<id>.json) -> route_of
    -> IntentParseResult -> existing agent dispatch
Layer-1 of the orchestrator (classify_dialogue_route incl. the 119 emergency reply, resolve_product_turn) still runs BEFORE this hook
and is not replaced here (owner decision; see README).

Safety order: Parser safety_marker is applied first and can never be overridden by the model (decide_v2 ORs it in).
On Sys1 timeout / error / malformed answer: Parser safety sign -> safety route, else -> clarify (UNCLEAR). Never the LLM, unless the
owner sets control "error_fallback": "llm" (handoff §8.5 — default is "safety_or_clarify").

Inert unless ALL hold: marker `.sys1-shadow-overlay` beside agents/ whose pkg_root == this package root; package root not under any
production root (marker `prod_roots` + built-ins); control arm != "off"; sys1_url is 127.0.0.1:8230-8239; frozen thresholds file valid.
Arms (control file re-read on mtime change -> switch without restart):
    off     original LLM parse_intent (pass-through)
    shadow  LLM result is returned unchanged; the P+Sys1 decision is computed in a background thread and only LOGGED (S3)
    sys1    P+Sys1 decision is used for `canary_pct`% of sessions (stable hash of the session key); the rest pass through
Control ($SYS1_DISPATCH_CONTROL or marker "control"): {"arm","canary_pct","sys1_url","model","frozen","timeout_s","trace",
    "trace_text","error_fallback","conversation_reply","skip_pain_gain"}
"""
from __future__ import annotations
import hashlib, importlib.util, json, os, re, sys, threading, time, urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve()
_PKG = _HERE.parents[2]
_MARKER = ".sys1-shadow-overlay"
_VENDOR = _HERE.with_name("sys1_vendor")
# Built-in production roots (from the vendored production orchestrator, analysis-source-20261013T163259Z). The marker written by
# shadowpatch adds the CURRENT list from prod_roots.json; both are checked.
_PROD_BUILTIN = ("/var/lib/jenkins/tomod/cosmetic-agent", "/var/lib/jenkins/tomod/cosmetic-agent-stable")
# SHA-256 of the vendored frozen modules (= gateB-colab/holdout/FREEZE.json). Mismatch -> overlay inert.
_VENDOR_SHA = {"kevlib.py": "1d2be226a720d72ae11569c4ae6afeaf7cb106424299facea2c2ad13a76cd92c",
               "parser_v2.py": "ce96e49826af855218d79e6a6cb2a089c7ecf7324e6e5acb4f3dff3346db36a3",
               "payload/kev_slot_parser.py": "84b5fffd647edee2d7d8c51ae08864c57bc69f1c8f765dc7bf5a66b67a6ac457"}
ROUTES = ("safety", "skin_tracking", "product", "knowledge", "conversation", "clarify")
_EMERGENCY_Q = {"type": "noul", "instructions": "這句話是否描述使用產品後出現的身體不適或可能危急的反應（腫脹、呼吸困難、喘、全身紅疹、頭暈、起水泡）？",
                "criteria": {"true": "描述了使用後的不適或危急反應", "false": "沒有描述不適反應"}}      # == gateb.EMERGENCY_Q (parity-tested)
_PARSER_FLAG_TO_INTENT = [("explicit_product_search", "find_product"), ("explicit_compare", "knowledge"), ("knowledge_marker", "knowledge"),
                          ("explicit_tracking", "record"), ("conversation_marker", "chitchat"), ("ambiguous_marker", "unclear")]   # == gateA
_CAT_RX = [("洗面乳", r"洗面乳|潔面"), ("化妝水", r"化妝水|爽膚水"), ("面膜", r"面膜"), ("精華液", r"精華"),
           ("乳霜", r"乳霜|面霜|保濕霜"), ("乳液", r"乳液"), ("防曬乳", r"防曬")]

_lock = threading.Lock()
_ctl_cache = {"mtime": None, "data": {}}
_frozen_cache = {"path": None, "mtime": None, "th": None, "err": None}
_mods = {}
_last = {}                        # (session key, text) -> route; read by early_dispatch / force_safety (thread-independent)
_LAST_MAX = 4096


# ----------------------------------------------------------------------------- guards
def _marker() -> dict:
    try: return json.loads((_PKG / _MARKER).read_text())
    except Exception: return {}


def _prod_roots():
    """None = marker's list is malformed (overlay disabled: fail closed)."""
    extra = _marker().get("prod_roots", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) and x for x in extra): return None
    return [Path(p).resolve() for p in list(_PROD_BUILTIN) + extra]


def _enabled_here() -> bool:
    roots = _prod_roots()
    if roots is None or any(_PKG == p or p in _PKG.parents for p in roots): return False
    m = _marker()
    try: return Path(m["pkg_root"]).resolve() == _PKG
    except Exception: return False


def _sha(p: Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()


def _vendor():
    """Load the frozen modules (verbatim copies) under private names; refuse if any byte differs."""
    if _mods: return _mods
    with _lock:
        if _mods: return _mods
        for rel, h in _VENDOR_SHA.items():
            if _sha(_VENDOR / rel) != h: raise RuntimeError(f"vendored {rel} differs from the frozen copy")
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, str(path)); mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod; spec.loader.exec_module(mod); return mod
        ksp = load("kev_slot_parser", _VENDOR / "payload" / "kev_slot_parser.py")   # parser_v2 imports it by this name
        _mods.update(K=load("sys1_kevlib", _VENDOR / "kevlib.py"), P2=load("sys1_parser_v2", _VENDOR / "parser_v2.py"), P1=ksp)
    return _mods


def _ctl() -> dict:
    """Never raises: any problem -> arm off (= the original LLM path)."""
    try: return _ctl_inner()
    except Exception: return {"arm": "off", "trace": "", "skip_pain_gain": False}


def _ctl_inner() -> dict:
    path = os.environ.get("SYS1_DISPATCH_CONTROL") or _marker().get("control")
    base = {"arm": "off", "canary_pct": 0, "sys1_url": "http://127.0.0.1:8232", "model": None, "frozen": None, "timeout_s": 2.0,
            "trace": "", "trace_text": False, "error_fallback": "safety_or_clarify", "conversation_reply": False, "skip_pain_gain": False}
    if path:
        try:
            m = os.stat(path).st_mtime_ns
            if m != _ctl_cache["mtime"]: _ctl_cache.update(mtime=m, data=json.loads(Path(path).read_text(encoding="utf-8")))
            base.update(_ctl_cache["data"])
        except Exception: base["arm"] = "off"
    if base["arm"] not in ("off", "shadow", "sys1"): base["arm"] = "off"
    if not _enabled_here(): base["arm"] = "off"
    if not re.fullmatch(r"http://127\.0\.0\.1:823\d/?", str(base["sys1_url"])): base["arm"] = "off"   # Sys1 range only
    if base["arm"] != "off" and (not base.get("model") or _frozen(base)[0] is None): base["arm"] = "off"
    try:
        c = float(base.get("canary_pct") or 0)
        if not 0 <= c <= 100: raise ValueError
        base["canary_pct"] = c
    except (TypeError, ValueError): base["arm"] = "off"
    if base["arm"] != "off":
        try: _vendor()
        except Exception: base["arm"] = "off"                     # vendored frozen modules missing / modified -> inert
    return base


def _frozen(ctl):
    """(thresholds, error). The file is written once by sys1/analyze_cand.py; its sha256 covers the thresholds."""
    p = ctl.get("frozen")
    if not p: return None, "no frozen thresholds configured"
    try:
        m = os.stat(p).st_mtime_ns
        if (_frozen_cache["path"], _frozen_cache["mtime"]) != (p, m):
            body = json.loads(Path(p).read_text(encoding="utf-8")); th = body["thresholds"]
            ok = hashlib.sha256(json.dumps(th, sort_keys=True).encode()).hexdigest() == body.get("sha256")
            _frozen_cache.update(path=p, mtime=m, th=th if ok else None, err=None if ok else "frozen sha256 mismatch", cand=body.get("cand"))
        return _frozen_cache["th"], _frozen_cache["err"]
    except Exception as e: return None, f"{type(e).__name__}: {e}"


def _trace(ctl: dict, rec: dict):
    p = ctl.get("trace")
    if not p: return
    try:
        with _lock, open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass


# ----------------------------------------------------------------------------- the Gate B policy (parity-tested against gateb/gateA/holdout)
def pk_bundle(p2: dict) -> dict:
    M = _vendor(); q = M["K"].full_bundle(p2, M["P2"].ING_ZH); q.pop("intent_choice", None); q["emergency"] = _EMERGENCY_Q; return q


def _p_noul(ans, name):
    try: return float(ans[name]["noul"])                   # noul high = yes (Sys1 contract; probe-checked per candidate)
    except Exception: return None


def kev_view(answers: dict, p2: dict) -> dict:
    M = _vendor(); K = M["K"]; a = answers or {}
    ps = _p_noul(a, "safety"); pi = {l: _p_noul(a, f"i_{l}") for l in K.INTENT_LABELS}
    con = (a.get("concern") or {}).get("choice"); roles = {}
    for i, _ in enumerate(p2["budget_candidates"][:3]):
        r = (a.get(f"role_{i}") or {}).get("choice")
        if r: roles[i] = r
    avoid = {k: _p_noul(a, f"avoid_{k}") for k in p2["ingredients"][:3] if f"avoid_{k}" in a}
    return {"ok": bool(a), "p_safety": ps, "p_intent": pi, "concern": con, "roles": roles, "p_avoid": avoid}


def parser_intents(p2): return sorted({lab for flag, lab in _PARSER_FLAG_TO_INTENT if p2.get(flag)})


def decide(p2: dict, kv: dict, th: dict) -> dict:
    K = _vendor()["K"]; labels = list(K.INTENT_LABELS)
    safety = bool(p2.get("safety_marker")) or (kv["p_safety"] is not None and kv["p_safety"] >= th["safety"])
    kev_int = {l for l in labels if kv["p_intent"].get(l) is not None and kv["p_intent"][l] >= th["intent"].get(l, .5)}
    p_int = set(parser_intents(p2)); intents = sorted(kev_int | p_int) if kv["ok"] else sorted(p_int)
    budget = None
    for i, cand in enumerate(p2["budget_candidates"][:3]):
        if cand["role"] == "range_lo": continue
        role = {"upper": "upper", "range_hi": "upper", "target": "target"}.get(cand["role"]) or kv["roles"].get(i)
        if role in ("upper", "target") and budget is None: budget = {"value": cand["value"], "role": role}
    avoid = sorted(set(p2["avoid_hint"]) & {k for k, v in kv["p_avoid"].items() if v is not None and v >= th["avoid"]}) if kv["ok"] else sorted(p2["avoid_hint"])
    concern = None if kv["concern"] in (None, K.NO_CONCERN) else kv["concern"]
    return {"safety": safety, "intents": intents, "budget": budget, "avoid": avoid, "concern": concern}


def decide_v2(p2, kv, p_em, th2):
    d = decide(p2, kv, th2["base"])
    em = p_em is not None and p_em >= th2["emergency"]
    d["emergency"] = em; d["safety"] = bool(d["safety"] or em)
    ints = [i for i in d["intents"] if i != "unclear"]
    d["intents"] = sorted(ints) if ints else ["unclear"]
    return d


def route_of(d):
    if not d: return "clarify"
    ints = set(d.get("intents") or [])
    if d.get("safety"): return "safety"
    if "record" in ints: return "skin_tracking"
    if ints & {"find_product", "price_comparison"}: return "product"
    if "knowledge" in ints: return "knowledge"
    if "chitchat" in ints: return "conversation"
    return "clarify"


def _valid(answers, q):
    """Malformed answers count as a Sys1 error (not silently treated as 'no')."""
    if not isinstance(answers, dict): return False
    for n, qq in q.items():
        v = answers.get(n)
        if not isinstance(v, dict): return False
        if qq["type"] == "noul":
            try:
                if not 0.0 <= float(v["noul"]) <= 1.0: return False
            except Exception: return False
        elif v.get("choice") not in qq["criteria"]: return False
    return True


def sys1_decide(text: str, ctl: dict) -> dict:
    """-> {"route","decision","p2","why","sys1_ms","server_ms","error"}; never raises."""
    t0 = time.perf_counter(); out = {"p2": {}, "sys1_ms": None, "server_ms": None, "error": None}; p2 = {}
    try:
        M = _vendor(); p2 = M["P2"].parse2(text); out["p2"] = p2; th, ferr = _frozen(ctl)
        if th is None: raise RuntimeError(f"frozen thresholds unavailable: {ferr}")
        q = pk_bundle(p2); body = json.dumps({"state": text, "model": ctl["model"], "questions": q}, ensure_ascii=False).encode()
        req = urllib.request.Request(str(ctl["sys1_url"]).rstrip("/") + "/v1/systemone", data=body, headers={"Content-Type": "application/json"})
        t = time.perf_counter()
        with urllib.request.urlopen(req, timeout=float(ctl["timeout_s"])) as r: ans = json.loads(r.read().decode())
        out["sys1_ms"] = round((time.perf_counter() - t) * 1000, 2); out["server_ms"] = ans.get("latency_ms")
        if not _valid(ans.get("answers"), q): raise ValueError("malformed Sys1 answers")
        kv = kev_view(ans["answers"], p2); d = decide_v2(p2, kv, _p_noul(ans["answers"], "emergency"), th)
        out.update(decision=d, route=route_of(d), why="sys1", p_safety=kv["p_safety"], p_emergency=_p_noul(ans["answers"], "emergency"))
    except Exception as e:
        safe = bool(p2.get("safety_marker"))
        out.update(error=f"{type(e).__name__}: {str(e)[:160]}", route="safety" if safe else "clarify", why="sys1_error:" + ("parser_safety" if safe else "clarify"),
                   decision={"safety": safe, "intents": ["unclear"] if not safe else ["knowledge"], "budget": None, "avoid": [], "concern": None, "emergency": False})
    out["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


# ----------------------------------------------------------------------------- IntentParseResult for the orchestrator
def _slots_from_parser(text: str) -> dict:
    p = _vendor()["P1"].parse(text)
    cat = next((c for c, rx in _CAT_RX if re.search(rx, text)), None)
    return {"skin_type": p.skin_type, "product_category": cat, "ingredients": list(p.ingredients or [])}


def build_intent(text: str, r: dict, with_slots: bool = True):
    from schemas.intent import IntentParseResult, IntentType, SkinType, ProductCategory, SkinConcern, calculate_ambiguity_score
    route, d = r["route"], r["decision"]; ints = set(d.get("intents") or [])
    if route == "product": itype = IntentType.PRICE_COMPARISON if ("price_comparison" in ints and "find_product" not in ints) else IntentType.FIND_PRODUCT
    elif route == "knowledge": itype = IntentType.KNOWLEDGE_QUERY
    elif route == "safety": itype = IntentType.FIND_PRODUCT                 # + force_safety() -> _handle_safety_query (same as fullpath-kev)
    else: itype = IntentType.UNCLEAR                                         # skin_tracking (early_dispatch) / conversation / clarify
    def enum(cls, v, default):
        try: return cls(v)
        except Exception: return default
    use = route in ("product", "safety", "knowledge"); s = _slots_from_parser(text) if (use and with_slots) else {}
    # avoided = model-confirmed avoid ∪ every Parser avoid hint (also beyond the 3 asked, and on the error path)
    avoid = set(d.get("avoid") or []) | set((r.get("p2") or {}).get("avoid_hint") or [])
    budget = d.get("budget")
    res = IntentParseResult(
        intent_type=itype, skin_type=enum(SkinType, s.get("skin_type"), SkinType.UNKNOWN) if use else SkinType.UNKNOWN,
        concern=[d["concern"]] if use and d.get("concern") in {c.value for c in SkinConcern} else [],
        product_category=enum(ProductCategory, s.get("product_category"), ProductCategory.UNKNOWN) if use else ProductCategory.UNKNOWN,
        budget_range=str(budget["value"]) if (route == "product" and budget) else None,
        # IntentParseResult has no "avoid" field: avoided ingredients are NEVER put into query_expansion (that would search FOR them).
        query_expansion=[k for k in (s.get("ingredients") or []) if k not in avoid], raw_query=text)
    return res.model_copy(update={"ambiguity_score": calculate_ambiguity_score(res)})


def _unclear(text):
    from schemas.intent import IntentParseResult, IntentType
    return IntentParseResult(intent_type=IntentType.UNCLEAR, raw_query=text, ambiguity_score=1.0)


def _session_key(orch) -> str:
    """Production RootAgentOrchestrator carries user_id + conversation_id (conversation_id defaults to "default" for every
    user, so it is never used alone). Test fixtures use state.session_id. Last resort: the orchestrator object."""
    uid, cid = getattr(orch, "user_id", None), getattr(orch, "conversation_id", None)
    if uid or cid: return f"u:{uid}|c:{cid}"
    sid = getattr(getattr(orch, "state", None), "session_id", None)
    if sid: return f"s:{sid}"
    return f"obj:{id(orch)}"


def in_canary(ctl, orch) -> bool:
    pct = max(0.0, min(100.0, float(ctl.get("canary_pct") or 0)))
    return int(hashlib.sha256(_session_key(orch).encode()).hexdigest()[:8], 16) % 10000 < pct * 100


def llm_route(res, text: str, orch) -> str:
    """Old router's route for the S3 comparison: same mapping as gateB-colab/prod_replay.from_intent + holdout.route_of."""
    try:
        kw = getattr(sys.modules.get(type(orch).__module__), "SAFETY_KEYWORDS", []) or []
        it = res.intent_type.value
        ints = [{"find_product": "find_product", "recommendation": "find_product", "price_comparison": "price_comparison",
                 "knowledge_query": "knowledge", "symptom_solving": "knowledge"}.get(it, "unclear")]
        try:
            from schemas.intent import calculate_routing_confidence
            if calculate_routing_confidence(res) < 0.5: ints = ["unclear"]
        except Exception: pass
        return route_of({"safety": it == "find_product" and any(k in text for k in kw), "intents": ints})
    except Exception as e: return f"error:{type(e).__name__}"


def _rec(ctl, text, orch):
    r = {"ts": time.time(), "arm": ctl["arm"], "q_sha": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text),
         "session": hashlib.sha256(_session_key(orch).encode()).hexdigest()[:12], "model": ctl.get("model"), "frozen_cand": _frozen_cache.get("cand")}
    if ctl.get("trace_text"): r["text"] = text
    return r


def _sys1_fields(r):
    d = r.get("decision") or {}
    return {"sys1_route": r["route"], "why": r["why"], "sys1_ms": r["sys1_ms"], "sys1_server_ms": r["server_ms"], "sys1_total_ms": r["total_ms"],
            "sys1_error": r["error"], "p_safety": r.get("p_safety"), "p_emergency": r.get("p_emergency"), "safety": d.get("safety"),
            "emergency": d.get("emergency"), "intents": d.get("intents"), "budget": d.get("budget"), "avoid": d.get("avoid"), "concern": d.get("concern"),
            "parser_safety_marker": bool(r["p2"].get("safety_marker"))}


def _remember(orch, text, route):
    with _lock:
        if len(_last) >= _LAST_MAX: _last.pop(next(iter(_last)))
        if route is None: _last.pop((_session_key(orch), text), None)
        else: _last[(_session_key(orch), text)] = route


def _recall(orch, text):
    with _lock: return _last.get((_session_key(orch), text))


def _forget(orch, text):
    with _lock: _last.pop((_session_key(orch), text), None)


def parse_intent(_orch=None, _fallback=None, **kw):
    """Drop-in for tools.agent.intent_parser.parse_intent (same keyword arguments)."""
    ctl = _ctl(); text = kw.get("user_query") or ""
    if ctl["arm"] == "off" or _orch is None or not isinstance(text, str):
        return _fallback(**kw)
    try: _remember(_orch, text, None)
    except Exception: pass
    if ctl["arm"] == "shadow":
        t0 = time.perf_counter(); res = _fallback(**kw)
        try:                                                     # nothing after the LLM call may change or lose its result
            llm_ms = round((time.perf_counter() - t0) * 1000, 2)
            rec = _rec(ctl, text, _orch); rec.update(used="llm", llm_route=llm_route(res, text, _orch), llm_ms=llm_ms)
            def bg():
                try: rec.update(_sys1_fields(sys1_decide(text, ctl)))
                except Exception as e: rec.update(sys1_error=f"overlay:{type(e).__name__}: {e}")
                _trace(ctl, rec)
            threading.Thread(target=bg, daemon=True).start()
        except Exception: pass
        return res
    try: canary = in_canary(ctl, _orch)
    except Exception: canary = False
    if not canary:
        try: rec = _rec(ctl, text, _orch); rec.update(used="llm", canary=False); _trace(ctl, rec)
        except Exception: pass
        return _fallback(**kw)
    rec = {"arm": ctl["arm"], "ts": time.time()}
    try:
        rec = _rec(ctl, text, _orch); r = sys1_decide(text, ctl); rec.update(used="sys1", canary=True, **_sys1_fields(r))
    except Exception as e:                                       # should not happen (sys1_decide never raises) — fail safe anyway
        safe = _parser_safe(text)
        r = {"route": "safety" if safe else "clarify", "p2": {}, "decision": {"safety": safe, "intents": ["knowledge"] if safe else ["unclear"], "avoid": []}}
        rec.update(used="sys1", overlay_error=f"{type(e).__name__}: {e}", sys1_route=r["route"])
    if r.get("error") and ctl.get("error_fallback") == "llm" and r["route"] != "safety":   # owner decision only (handoff §8.5)
        rec["used"] = "llm_after_error"; _trace(ctl, rec); return _fallback(**kw)
    try: intent = build_intent(text, r)
    except Exception as e:
        rec["build_error"] = f"{type(e).__name__}: {e}"
        if r["route"] != "safety": r["route"] = "clarify"
        rec["sys1_route"] = r["route"]
        try: intent = build_intent(text, r, with_slots=False) if r["route"] == "safety" else _unclear(text)
        except Exception: r["route"] = "clarify"; rec["sys1_route"] = "clarify"; intent = _unclear(text)
    if r["route"] == "conversation" and ctl.get("conversation_reply"): rec["conversation_reply_reserved"] = True   # not implemented: owner decision
    try: _remember(_orch, text, r["route"])
    except Exception: pass
    _trace(ctl, rec)
    return intent


def _parser_safe(text):
    try: return bool(_vendor()["P2"].parse2(text).get("safety_marker"))
    except Exception: return False


def early_dispatch(orch, user_input: str, intent):
    """skin_tracking has no IntentType: hand over to the Skin-Tracking agent directly."""
    try:
        if _recall(orch, user_input) != "skin_tracking": return None
        _forget(orch, user_input)
    except Exception: return None
    orch.state.active_flow = "skin_record"
    return orch._handle_skin_tracking_input(user_input)


def force_safety(orch, intent) -> bool:
    try: return _recall(orch, intent.raw_query or "") == "safety"
    except Exception: return False


def skip_pain_gain() -> bool:
    c = _ctl(); return c.get("arm") == "sys1" and bool(c.get("skip_pain_gain"))
