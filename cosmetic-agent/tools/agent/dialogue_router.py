"""Deterministic routing for obvious, high-risk dialogue cases.

This module intentionally contains no model call.  It handles only requests
whose destination must not depend on a probabilistic intent decision; all
other messages continue through the existing intent parser.
"""

from __future__ import annotations

import re
from typing import Any, Optional, TypedDict


class DialogueRoute(TypedDict, total=False):
    route: str
    reason: str
    action: str


_INTERNAL_PREFIXES = ("__", "cycle_", "confirm_")
_VALID_FLOWS = {"product_search", "skin_record", "skin_consult", "auto"}
_SUPPORTED_ACTIONS = frozenset({
    "more_products",
    "replace_batch",
    "record_correction",
    "record_location",
    "record_severity",
    "record_confirm",
    "cancel",
})
_ACTION_ALIASES = {
    "cycle_more": "more_products",
    "more": "more_products",
    "more_products": "more_products",
    "cycle_replace_batch": "replace_batch",
    "replace_batch": "replace_batch",
    "record_confirm": "record_confirm",
    "record_correction": "record_correction",
    "record_location": "record_location",
    "record_severity": "record_severity",
    "cancel": "cancel",
}

_SOCIAL_ONLY_TERMS = {
    "嗨",
    "嗨嗨",
    "哈囉",
    "哈啰",
    "你好",
    "您好",
    "早安",
    "午安",
    "晚安",
    "在嗎",
    "有人嗎",
    "hi",
    "hello",
    "hey",
    "謝謝",
    "感謝",
    "謝啦",
    "好的",
    "了解",
    "ok",
    "okay",
}

_INCOMPLETE_ONLY_TERMS = {
    "嗯",
    "呃",
    "那個",
    "就是",
    "我想",
    "我想要",
    "我最近",
    "最近",
    "我的皮膚",
    "我的膚況",
    "想問",
    "想問一下",
    "請問",
    "因為",
    "可是",
    "但是",
    "如果",
    "然後",
    "所以",
    "可以幫我",
    "能不能幫我",
    "我有點",
    "最近有點",
    "我不知道怎麼說",
}

_INCOMPLETE_ENDINGS = (
    "我想",
    "我想要",
    "想問",
    "想問一下",
    "請問",
    "因為",
    "可是",
    "但是",
    "如果",
    "然後",
    "所以",
    "可以幫我",
    "能不能幫我",
    "我有點",
    "最近有點",
)

_EMERGENCY_CUES = (
    "呼吸困難",
    "呼吸不順",
    "呼吸不太順",
    "呼吸很不順",
    "呼吸有點困難",
    "呼吸急促",
    "喘不過氣",
    "喘不動",
    "吸不到氣",
    "無法呼吸",
    "呼吸不了",
    "喉嚨緊",
    "喉頭緊",
    "喉嚨腫",
    "喉頭腫",
    "舌頭腫",
    "嘴唇腫",
    "唇部腫",
)

_FACIAL_SWELLING_CUES = (
    "眼皮腫",
    "眼周腫",
    "眼睛腫",
    "嘴唇腫",
    "唇部腫",
    "舌頭腫",
)

_NEGATION_PATTERN = re.compile(
    r"(?:沒有|並沒有|並無|無|未|不是|不會|否認).{0,5}$"
)

_RECORD_TERMS = (
    "記錄",
    "紀錄",
    "追蹤",
    "存下",
    "保存",
    "登記",
)

_SKIN_TERMS = (
    "膚",
    "皮膚",
    "臉",
    "痘",
    "粉刺",
    "泛紅",
    "紅腫",
    "乾燥",
    "出油",
    "脫皮",
    "刺痛",
    "發癢",
    "毛孔",
    "黑頭",
    "黑眼圈",
    "眼周",
    "眼皮",
)

_PRODUCT_TERMS = (
    "產品",
    "商品",
    "保養品",
    "洗面乳",
    "乳霜",
    "乳液",
    "精華",
    "防曬",
    "面膜",
    "化妝水",
    "推薦",
    "購買",
    "哪一款",
    "比較",
    "預算",
    "價格",
)

_VAGUE_SKIN_PHRASES = (
    "皮膚怪怪的",
    "膚況怪怪的",
    "皮膚不太對",
    "膚況不太對",
    "膚況不太好",
    "皮膚有點問題",
    "膚況有點問題",
)

_SYMPTOM_TERMS = (
    "泛紅",
    "紅腫",
    "脫皮",
    "刺痛",
    "發癢",
    "很癢",
    "乾燥",
    "很乾",
    "乾乾的",
    "爆痘",
    "長痘",
    "粉刺",
    "灼熱",
    "疼痛",
    "過敏",
    "色素沉澱",
    "出油",
    "很油",
    "油油的",
    "毛孔",
)

_KNOWLEDGE_TERMS = (
    "a酸",
    "a醇",
    "水楊酸",
    "果酸",
    "壬二酸",
    "維他命c",
    "維生素c",
    "神經醯胺",
    "菸鹼醯胺",
    "玻尿酸",
    "視黃醇",
    "成分",
)

_CONSULT_TERMS = (
    "怎麼辦",
    "為什麼",
    "原因",
    "怎麼處理",
    "如何改善",
    "能不能用",
    "可以用嗎",
    "副作用",
    "功用",
    "是什麼",
    "怎麼用",
    "一起用",
)

_RECALL_SCOPE_TERMS = (
    "本對話",
    "這個對話",
    "這段對話",
)

_RECALL_QUESTION_TERMS = (
    "先前",
    "之前",
    "剛才",
    "說過",
    "提過",
    "記得",
    "測試代號",
)

_RECALL_NOISE_TERMS = (
    "本對話",
    "這個對話",
    "這段對話",
    "先前",
    "之前",
    "剛才",
    "明確",
    "曾經",
    "曾",
    "說過",
    "提過",
    "記得",
    "只根據",
    "陳述",
    "回答",
    "若沒有",
    "如果沒有",
    "就說",
    "什麼",
    "是否",
    "哪一",
)

_USER_HISTORY_PREFIX = "使用者先前訊息："


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？、；;：:～~…]+", "", text).lower()


def _normalize_flow(value: str | None) -> str:
    flow = str(value or "").strip().lower()
    return flow if flow in _VALID_FLOWS else "auto"


def normalize_dialogue_action(value: str | None) -> Optional[str]:
    """Normalize API actions and legacy quick-reply values to one contract."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    action = _ACTION_ALIASES.get(raw, raw)
    return action if action in _SUPPORTED_ACTIONS else None


def _history_text(history: list[dict[str, Any]] | None) -> str:
    recent = []
    for turn in (history or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content")
        if isinstance(content, str) and content.strip():
            recent.append(content.strip())
    return _normalize(" ".join(recent))


def _structured_action_from_text(
    text: str,
    conversation_flow: str,
    current_step: str,
    structured_state: dict[str, Any] | None,
) -> Optional[str]:
    normalized = _normalize(text)
    state = structured_state or {}
    state_flow = _normalize_flow(state.get("active_flow"))
    step = str(current_step or state.get("current_step") or "").lower()
    intent_type = str(state.get("intent_type") or "")
    intent_confirm_context = (
        step in {"intent_confirmation", "pain_gain_confirm"}
        or bool(state.get("is_waiting_confirm"))
    )
    product_context = (
        conversation_flow == "product_search"
        or state_flow == "product_search"
        or step.startswith("product")
        or intent_type in {"find_product", "recommendation", "price_comparison"}
        or bool(state.get("has_products"))
    )
    record_context = (
        conversation_flow == "skin_record"
        or state_flow == "skin_record"
        or step.startswith("skin_tracking")
        or bool(state.get("skin_session_active"))
    )

    if normalized in {"取消", "取消生成", "停止", "停止處理", "算了", "不要了", "cancel"}:
        return "cancel"

    if product_context:
        if any(term in normalized for term in ("換一批", "下一批", "不要重複", "重新換一批")):
            return "replace_batch"
        if any(term in normalized for term in ("看更多商品", "看更多", "再多給我幾個", "再多幾個", "還有其他商品")):
            return "more_products"

    if record_context and not intent_confirm_context:
        if re.search(r"(?:嚴重度|程度).{0,4}(?:改成|改為|是)?[1-5](?:/5|分)?", normalized):
            return "record_severity"
        if any(term in normalized for term in ("不是", "改成", "改為", "應該是", "其實是")):
            return "record_correction"
        if normalized in {"confirm_yes", "對就記這些部位", "就記這些", "就記這些部位", "確認紀錄", "確認儲存", "儲存這筆", "確認", "沒錯", "是的"}:
            return "record_confirm"

    return None


def _is_social_only(text: str) -> bool:
    """Match a complete social turn, never a greeting plus a real request."""
    return _normalize(text) in _SOCIAL_ONLY_TERMS


def _looks_incomplete(text: str) -> bool:
    """Catch short abandoned utterances before they reach intent inference."""
    normalized = _normalize(text)
    if normalized in _INCOMPLETE_ONLY_TERMS:
        return True
    if len(normalized) > 16:
        return False
    return any(normalized.endswith(ending) for ending in _INCOMPLETE_ENDINGS)


def _has_non_negated_cue(text: str, cues: tuple[str, ...]) -> bool:
    for cue in cues:
        for match in re.finditer(re.escape(cue), text):
            prefix = text[max(0, match.start() - 10):match.start()]
            if not _NEGATION_PATTERN.search(prefix):
                return True
    return False


def _has_non_negated_emergency(text: str) -> bool:
    if _has_non_negated_cue(text, _EMERGENCY_CUES):
        return True
    # Eye/facial swelling alone may have several causes.  Combined with an
    # airway symptom it must be intercepted before any LLM call.
    return (
        _has_non_negated_cue(text, _FACIAL_SWELLING_CUES)
        and _has_non_negated_cue(text, _EMERGENCY_CUES)
    )


def _is_explicit_conversation_recall(text: str) -> bool:
    normalized = _normalize(text)
    scoped = any(term in normalized for term in _RECALL_SCOPE_TERMS)
    question_form = any(
        term in normalized
        for term in ("嗎", "什麼", "哪", "是否", "有沒有", "是不是")
    ) or any(mark in text for mark in ("?", "？"))
    return (
        (scoped and _looks_like_recall_question(text))
        or ("測試代號" in normalized and question_form)
    )


def _looks_like_recall_question(text: str) -> bool:
    normalized = _normalize(text)
    recall_word = any(
        term in normalized
        for term in _RECALL_QUESTION_TERMS
    )
    question_form = any(
        term in normalized
        for term in ("嗎", "什麼", "哪", "是否", "有沒有", "是不是")
    ) or any(mark in text for mark in ("?", "？"))
    return recall_word and question_form


def _topic_bigrams(text: str) -> set[str]:
    normalized = _normalize(text)
    for term in _RECALL_NOISE_TERMS:
        normalized = normalized.replace(term, "")
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)
    return {
        normalized[index:index + 2]
        for index in range(max(0, len(normalized) - 1))
    }


def conversation_recall_reply(
    text: str,
    history_context: str,
) -> Optional[str]:
    """Answer explicit same-conversation recall without another model call."""
    raw = (text or "").strip()
    if not raw or not _is_explicit_conversation_recall(raw):
        return None

    prior_messages = []
    for line in (history_context or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(_USER_HISTORY_PREFIX):
            continue
        message = stripped[len(_USER_HISTORY_PREFIX):].strip()
        if not message or _looks_like_recall_question(message):
            continue
        prior_messages.append(message)

    query_terms = _topic_bigrams(raw)
    ranked = sorted(
        (
            (len(query_terms & _topic_bigrams(message)), index, message)
            for index, message in enumerate(prior_messages)
        ),
        reverse=True,
    )
    if ranked and ranked[0][0] >= 2:
        statement = ranked[0][2][:300]
        return f"你在本對話中曾說：「{statement}」"

    return "本對話沒有提過。"


def classify_dialogue_route(
    text: str,
    *,
    conversation_flow: str | None = None,
    current_step: str | None = None,
    structured_state: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    action: str | None = None,
) -> Optional[DialogueRoute]:
    """Return a deterministic route/action only when the request is unambiguous.

    The optional keyword arguments are internal, structured context.  They are
    deliberately separate from user text so a display label cannot masquerade
    as an action and active card state can resolve short follow-up messages.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    normalized = _normalize(raw)
    flow = _normalize_flow(conversation_flow)
    state = structured_state or {}
    step = str(current_step or state.get("current_step") or "idle")

    if _has_non_negated_emergency(normalized):
        return {"route": "emergency", "reason": "airway_or_angioedema"}

    structured_action = (
        normalize_dialogue_action(action)
        or _structured_action_from_text(action or "", flow, step, state)
        or normalize_dialogue_action(raw)
        or _structured_action_from_text(raw, flow, step, state)
    )
    if structured_action:
        return {
            "route": "action",
            "reason": "structured_dialogue_action",
            "action": structured_action,
        }

    if raw.lower().startswith(_INTERNAL_PREFIXES):
        return None

    if _is_social_only(raw):
        return {"route": "small_talk", "reason": "social_only"}

    if _looks_incomplete(raw):
        return {"route": "incomplete", "reason": "unfinished_utterance"}

    has_record = any(term in normalized for term in _RECORD_TERMS)
    has_skin = any(term in normalized for term in _SKIN_TERMS)
    has_product = any(term in normalized for term in _PRODUCT_TERMS)
    has_symptom = any(term in normalized for term in _SYMPTOM_TERMS)
    has_knowledge = any(term in normalized for term in _KNOWLEDGE_TERMS)
    has_consult = any(term in normalized for term in _CONSULT_TERMS)
    is_question = any(mark in raw for mark in ("?", "？")) or any(
        term in normalized for term in ("嗎", "什麼", "哪", "如何", "怎麼", "為何", "原因")
    )
    shopping_request = any(
        term in normalized
        for term in ("推薦", "推建", "想買", "我要買", "想找", "幫我找", "選購", "哪款")
    )

    if has_record and has_skin and has_product:
        return {"route": "multi_intent", "reason": "record_and_product"}

    if has_record and has_skin:
        return {"route": "skin_record", "reason": "explicit_skin_record"}

    history_text = _history_text(history)

    if flow == "skin_record":
        if has_product and shopping_request and not has_record:
            return None
        if (has_consult or is_question or has_knowledge) and not has_record:
            return {"route": "skin_consult", "reason": "record_flow_topic_switch"}
        return {"route": "skin_record", "reason": "active_skin_record_flow"}

    if flow == "skin_consult":
        if has_product and shopping_request:
            return None
        if len(normalized) <= 6 and (has_skin or has_symptom) and not (has_consult or is_question):
            return {"route": "clarify", "reason": "bare_consult_topic"}
        return {"route": "skin_consult", "reason": "active_skin_consult_flow"}

    if flow == "product_search":
        price_question = any(
            term in normalized
            for term in ("價格", "便宜", "划算", "單價", "cp值", "價差")
        )
        product_detail_question = any(
            term in normalized
            for term in (
                "功能", "功效", "作用", "成分", "怎麼用", "如何使用", "使用方法",
                "適合什麼膚質", "適合哪種膚質", "適合何種膚質", "適合敏感肌嗎",
                "適合乾肌嗎", "適合油肌嗎", "注意事項", "會刺激嗎", "安全嗎", "副作用",
            )
        )
        product_reference = any(
            term in normalized
            for term in (
                "這款", "這個商品", "這個產品", "這瓶", "這罐", "它", "剛剛那款", "上一款",
            )
        )
        product_context = (
            has_product
            or bool(state.get("has_products"))
            or step == "product_results"
        )
        brand_refinement = (
            any(term in normalized for term in ("品牌", "牌子", "大牌"))
            and any(
                term in normalized
                for term in (
                    "比較有名", "比較知名", "更有名", "更知名",
                    "知名一點", "大牌一點", "有名的品牌", "有名的牌子",
                    "更多", "其他", "換", "再給我",
                )
            )
        )
        constraint_refinement = any(
            term in normalized
            for term in (
                "再便宜", "便宜一點", "清爽一點", "不要太厚重",
                "不要厚重", "不黏", "其他款", "更多款", "再給我",
                "換一款", "換一批", "換成", "改成",
            )
        )
        if product_context and (brand_refinement or constraint_refinement):
            return {
                "route": "product_search",
                "reason": "product_refinement_followup",
            }

        if product_detail_question and (
            has_product
            or bool(state.get("has_products"))
            or product_reference
            or step == "product_results"
        ):
            return {"route": "product_search", "reason": "product_detail_followup"}
        if (has_knowledge or has_consult or is_question) and not shopping_request and not price_question:
            return {"route": "skin_consult", "reason": "product_flow_topic_switch"}
        return {"route": "product_search", "reason": "active_product_search_flow"}

    if (
        normalized in {"為什麼", "怎麼辦", "這樣正常嗎", "所以我這種膚質呢"}
        and history_text
        and any(term in history_text for term in (*_SKIN_TERMS, *_SYMPTOM_TERMS, *_KNOWLEDGE_TERMS))
    ):
        return {"route": "skin_consult", "reason": "contextual_consult_followup"}

    profile_only = (
        any(term in normalized for term in ("歲", "油肌", "乾肌", "混合肌", "敏感肌", "中性肌"))
        and "預算" in normalized
        and not shopping_request
        and not any(term in normalized for term in ("洗面乳", "乳霜", "乳液", "精華", "防曬", "面膜", "化妝水"))
    )
    if profile_only:
        return {"route": "clarify", "reason": "profile_without_goal"}

    if (
        any(phrase in normalized for phrase in _VAGUE_SKIN_PHRASES)
        and not has_symptom
    ):
        return {"route": "clarify", "reason": "vague_skin_request"}

    if not has_product and (has_symptom or (has_knowledge and has_consult)):
        return {"route": "skin_consult", "reason": "clear_skin_consult"}

    return None
