#!/usr/bin/env python3
"""prod_replay — replay of the PRODUCTION intent path (cosmetic-agent), using the production code itself (vendor_prod/, unmodified
copies from source-bundle.zip, analysis-source-20261013T163259Z; see vendor_prod/SHA256SUMS). Only Terra is replaced by a local LLM.

Order replayed (agents/root/orchestrator.py, _process_current_turn):
  :2338 classify_dialogue_route  → 'emergency' returns the 119 reply (the ONLY dedicated emergency handling)
  :2586 guard_input              → NOT replayed (rule part blocks injection/abuse/off-topic; ambiguous cases call Terra)
  :2703 resolve_product_turn     → no LLM: FIND_PRODUCT built from rule patches (or skin_consult / category clarification)
  :2717 layer-1 route branches   → small_talk / incomplete / clarify / multi_intent canned replies; skin_record → tracking agent;
                                   skin_consult → KNOWLEDGE_QUERY without parse_intent; product_search / None → _handle_new_input
  :2833 parse_intent             → intent_parser._fast_parse_obvious_product_query (no LLM) else the LLM call
                                   (SYSTEM_PROMPT + _build_current_user_content + INTENT_JSON_SCHEMA, strict, max_output_tokens 400)
                                   → _intent_result_from_raw (guards, concern sanitising, score recomputation)
  :2923 routing_confidence < 0.5 → routing clarification (scored here as intents=[unclear])
  :1969 _dispatch                → safety consult ONLY when intent == FIND_PRODUCT and raw_query contains SAFETY_KEYWORDS (:141)
Conversation state = a fresh conversation (idle, active_flow auto, no products, no history, no long-term memory).
Not replayed: input guard, profile interaction, the 'low' reasoning-effort retry (:1297), memory enrichment, the agents' own answers."""
from __future__ import annotations
import os, re, sys, time, types

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.join(HERE, "vendor_prod")
if VP not in sys.path: sys.path.insert(0, VP)
try:
    import dotenv  # noqa: F401  (intent_parser calls load_dotenv() at import)
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: False)
os.environ.setdefault("OPENAI_API_KEY", "")
from tools.agent.dialogue_router import classify_dialogue_route          # noqa: E402
from tools.agent.product_turn_resolver import resolve_product_turn      # noqa: E402
from tools.agent import intent_parser as IP                             # noqa: E402
from schemas.intent import IntentParseResult, IntentType                # noqa: E402

_ORCH = open(os.path.join(VP, "agents", "root", "orchestrator.py"), encoding="utf-8").read()
SAFETY_KEYWORDS = eval(re.search(r"SAFETY_KEYWORDS = (\[.*?\])", _ORCH, re.S).group(1))   # literal list in the vendored file
# orchestrator._routing_context() for a fresh conversation
ROUTING_CTX = {"active_flow": "auto", "current_step": "idle", "intent_type": None, "product_category": None, "has_products": False,
               "seen_product_count": 0, "is_waiting_confirm": False, "skin_session_active": False}
TO_GATE = {"find_product": "find_product", "recommendation": "find_product", "price_comparison": "price_comparison",
           "knowledge_query": "knowledge", "symptom_solving": "knowledge", "unclear": "unclear"}
CANON = ["保濕", "緊緻", "抗皺", "亮白", "透亮", "逆齡", "舒緩", "舒敏", "抗痘", "控油", "毛孔", "美白", "淡斑"]

SYSTEM_PROMPT = IP.SYSTEM_PROMPT
JSON_SCHEMA = IP.INTENT_JSON_SCHEMA
MAX_OUTPUT_TOKENS = IP.OPENAI_INTENT_MAX_OUTPUT_TOKENS


def user_content(text: str) -> str:
    return IP._build_current_user_content(user_query=text, long_term_summary="", routing_context=ROUTING_CTX)


def _budget(v):
    if v in (None, "", "null"): return None
    import gemma_arms as GM
    b = GM._budget_value(v)
    return {"value": b, "role": "upper"} if b else None          # production documents budget_range as「預算上限」


def from_intent(res: IntentParseResult, text: str) -> dict:
    it = res.intent_type.value
    d = {"intents": [TO_GATE.get(it, "unclear")], "budget": _budget(res.budget_range), "avoid": [], "emergency": False,
         "safety": it == "find_product" and any(k in text for k in SAFETY_KEYWORDS)}
    con = [c for c in (res.concern or []) if c in CANON]
    d["concern"] = con[0] if con else None
    if float(res.routing_confidence or 0.0) < 0.5: d["intents"] = ["unclear"]            # :2923 routing clarification
    return d


def deterministic(text: str):
    """-> (path, decision | None, ms). decision None = production would call the LLM for this sentence."""
    t0 = time.perf_counter()
    r = classify_dialogue_route(text, conversation_flow="auto", current_step="idle", structured_state=ROUTING_CTX, history=[], action=None)
    route = (r or {}).get("route")
    base = {"safety": False, "intents": [], "budget": None, "avoid": [], "concern": None, "emergency": False}
    out = None
    if route == "emergency":
        path, out = "L1:emergency", dict(base, safety=True, emergency=True, intents=["knowledge"])
    elif route == "action":
        path, out = "L1:action", dict(base, intents=["unclear"])
    else:
        pt = resolve_product_turn(text, conversation_flow="auto", current_step="idle", structured_state=ROUTING_CTX, action=None)
        if not pt["requires_intent_parser"] and route not in {"small_talk", "skin_record", "multi_intent"}:
            path = f"resolver:{pt['reason']}"
            if pt["route"] == "skin_consult": out = dict(base, intents=["knowledge"])
            elif pt.get("needs_clarification"): out = dict(base, intents=["unclear"])
            else:                                                                     # orchestrator._handle_resolved_product_turn
                data = IntentParseResult(intent_type=IntentType.FIND_PRODUCT, raw_query=text).model_dump()
                data.update(pt.get("intent_patch") or {}); data["intent_type"] = "find_product"; data["raw_query"] = text
                res = IntentParseResult(**data)
                from schemas.intent import calculate_routing_confidence
                res.routing_confidence = calculate_routing_confidence(res)
                out = from_intent(res, text)
        elif route == "small_talk": path, out = "L1:small_talk", dict(base, intents=["chitchat"])
        elif route in ("incomplete", "clarify"): path, out = f"L1:{route}", dict(base, intents=["unclear"])
        elif route == "multi_intent": path, out = "L1:multi_intent", dict(base, intents=["record", "find_product"])
        elif route == "skin_record": path, out = "L1:skin_record", dict(base, intents=["record"])
        elif route == "skin_consult": path, out = "L1:skin_consult", dict(base, intents=["knowledge"])
        else:
            fp = IP._fast_parse_obvious_product_query(text)
            if fp is not None: path, out = "fast_path", from_intent(fp, text)
            else: path = "LLM"
    return path, out, (time.perf_counter() - t0) * 1000


def llm_decision(raw: str | None, text: str):
    """production post-processing of one LLM reply -> (decision, error). On failure production returns UNCLEAR (parse_intent)."""
    try:
        res = IP._intent_result_from_raw(raw or "", text, ROUTING_CTX)
        return from_intent(res, text), None
    except Exception as e:
        return {"safety": False, "intents": ["unclear"], "budget": None, "avoid": [], "concern": None, "emergency": False}, type(e).__name__
