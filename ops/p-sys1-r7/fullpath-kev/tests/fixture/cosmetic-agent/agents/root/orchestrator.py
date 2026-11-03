"""Test fixture: reproduces the exact anchor lines of the production orchestrator, nothing else."""
import time
from tools.agent.intent_parser import OPENAI_API_KEY, OPENAI_MODEL, parse_intent
from schemas.intent import IntentParseResult, IntentType, calculate_routing_confidence, calculate_task_completeness

SAFETY_KEYWORDS = ['適合嗎', '能用嗎']

class _S:
    history = []; intent = None; active_flow = ""; pain_gain = {}
    def add_turn(self, *a): pass

class Orchestrator:
    model = "x"
    def __init__(self): self.state = _S(); self.log = []
    def _routing_context(self): return {}
    def _intent_memory_context(self): return ""
    def _log(self, *a): self.log.append(a)
    def _handle_skin_tracking_input(self, t): return {"stage": "skin_tracking", "reply": "tracked"}
    def _ask_clarification(self, d): return {"stage": "clarify:" + d}
    def _do_dispatch(self, intent): return {"stage": "dispatch", "agent": self._dispatch(intent)}
    def _handle_safety_query(self, i): return "safety_agent"
    def _handle_find_product(self, i): return "product_agent:%s:%s:%s" % (i.skin_type.value, i.product_category.value, i.budget_range)
    def _handle_knowledge(self, i): return "knowledge_agent"

    def _handle_new_input(self, user_input, ignore_history=False, routing_context=None, allow_reasoning_fallback=True):
        long_term = ""
        intent_started = time.perf_counter()
        intent = parse_intent(
            user_query=user_input,
            session_history=([] if ignore_history else self.state.history[:-1]),
            long_term_summary=long_term,
            model=self.model,
            routing_context=routing_context or self._routing_context(),
            allow_reasoning_fallback=allow_reasoning_fallback,
        )
        intent = intent.model_copy(update={"routing_confidence": calculate_routing_confidence(intent), "task_completeness": calculate_task_completeness(intent)})
        self.state.intent = intent
        if intent.intent_type in (IntentType.FIND_PRODUCT,):
            pass
        elif len(user_input) > 20:
            pain_gain_started = time.perf_counter()
            self.state.pain_gain = {"analysed": True}

        if intent.routing_confidence < 0.5:
            self._log("STEP", "目標 Agent 不明，先確認使用者要做的事")
            return self._ask_clarification("routing")
        return self._do_dispatch(intent)

    def _handle_confirm_reply(self, user_input):
        self._log("CONFIRM", "使用者要修正需求")
        intent = parse_intent(
            user_query=user_input,
            session_history=self.state.history[:-1],
            long_term_summary=self._intent_memory_context(),
            model=self.model,
            routing_context=self._routing_context(),
            allow_reasoning_fallback=True,
        )
        return intent

    def _dispatch(self, intent):
        if (intent.intent_type == IntentType.FIND_PRODUCT and
                any(kw in (intent.raw_query or '') for kw in SAFETY_KEYWORDS)):
            return self._handle_safety_query(intent)
        if intent.intent_type == IntentType.FIND_PRODUCT:
            return self._handle_find_product(intent)
        if intent.intent_type == IntentType.KNOWLEDGE_QUERY:
            return self._handle_knowledge(intent)
        return "none"
