"""Deterministic product-turn routing before the general intent parser.

This module intentionally has no model or network dependency. It handles the
parts of a product conversation that are already unambiguous from structured
state. The caller uses the general intent parser only when requested here.
"""

from __future__ import annotations

import re
from typing import Any, Literal, TypedDict


ResolvedRoute = Literal["product_search", "skin_consult", "unresolved"]


class ProductTurnResolution(TypedDict):
    route: ResolvedRoute
    action: str | None
    reason: str
    requires_intent_parser: bool
    inherit_context: bool
    needs_clarification: bool
    intent_patch: dict[str, Any]


_ACTION_ALIASES = {
    "more": "more_products",
    "cycle_more": "more_products",
    "more_products": "more_products",
    "replace": "replace_batch",
    "cycle_replace_batch": "replace_batch",
    "replace_batch": "replace_batch",
    "cancel": "cancel",
}

_CATEGORY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("洗面乳", ("洗面乳", "洗面慕斯", "潔面乳", "潔面", "洗臉產品")),
    ("化妝水", ("化妝水", "爽膚水", "柔膚水")),
    ("面膜", ("面膜",)),
    ("pore_strip", ("粉刺貼", "粉刺貼片")),
    ("exfoliant", ("去角質凝膠", "去角質產品", "去角質")),
    ("makeup_remover", ("卸妝產品", "卸妝油", "卸妝水", "卸妝乳", "卸妝膏", "卸妝")),
    ("eye_care", ("眼霜", "眼周產品", "眼部精華", "眼部凝膠")),
    ("lip_care", ("護唇產品", "護唇膏", "潤唇膏", "護唇")),
    ("spot_care", ("痘痘凝膠", "痘痘凝露", "局部抗痘產品", "抗痘痘產品", "抗痘產品")),
        ("acne_patch", ("痘痘貼", "痘貼", "acnepatch")),
    ("精華液", ("精華液", "精華")),
    ("乳液", ("乳液",)),
    ("乳霜", ("乳霜", "面霜")),
    ("保濕棉片", ("保濕棉片", "棉片")),
    ("防曬乳", ("防曬乳", "防曬霜", "防曬")),
)

_PRODUCT_INTENT_TYPES = {
    "find_product",
    "recommendation",
    "price_comparison",
}

_SHOPPING_MARKERS = (
    "推薦", "推建", "想買", "我要買", "想找", "幫我找", "幫我選",
    "幫我挑", "選一個", "挑一個", "哪款", "哪一款", "有哪些", "用什麼",
)

_KNOWLEDGE_MARKERS = (
    "成分", "功效", "怎麼用", "如何用", "使用方法", "副作用", "為什麼",
    "原因", "差別", "好用嗎", "可以嗎", "會不會", "評價", "多少錢", "哪裡買",
)

_MORE_MARKERS = (
    "看更多", "更多商品", "更多款", "再多給", "再多幾個", "再推薦幾個",
    "還有其他商品", "還有其他款",
)

_REPLACE_MARKERS = (
    "換一批", "換批", "重新換一批", "不要重複", "換一瓶", "換一個", "換一款",
)

_REFINEMENT_MARKERS = (
    "清爽", "不黏", "不要黏", "滋潤", "凝膠", "乳霜質地", "油類", "便宜",
    "預算", "元", "塊", "敏感肌", "乾肌", "油肌", "混合肌", "保濕", "抗痘",
    "痘痘", "美白", "淡斑", "毛孔", "抗老", "舒緩", "換成", "改成",
)


def _compact(value: str | None) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;]", "", str(value or "").lower())


def _find_category(text: str) -> str | None:
    for value, aliases in _CATEGORY_ALIASES:
        if any(alias in text for alias in aliases):
            return value
    return None


def _chinese_number_to_int(value: str) -> int | None:
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            return None
    result = total + current
    return result or None


def _extract_budget(text: str) -> str | None:
    arabic = re.search(r"(\d{2,6})\s*(?:元|塊)", text)
    if arabic:
        return arabic.group(1)
    chinese = re.search(r"([零〇一二兩三四五六七八九十百千]+)\s*(?:元|塊)", text)
    if chinese:
        parsed = _chinese_number_to_int(chinese.group(1))
        if parsed is not None:
            return str(parsed)
    return None


def _intent_patch(text: str) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    category = _find_category(text)
    if category:
        patch["product_category"] = category

    budget = _extract_budget(text)
    if budget:
        patch["budget_range"] = budget

    if any(term in text for term in ("清爽", "不黏", "不要黏", "輕盈")):
        patch["texture_preference"] = "清爽"
    elif "滋潤" in text:
        patch["texture_preference"] = "滋潤"
    elif "凝膠" in text:
        patch["texture_preference"] = "凝膠"
    elif "乳霜質地" in text:
        patch["texture_preference"] = "乳霜"
    elif "油類" in text or "油狀" in text:
        patch["texture_preference"] = "油類"

    skin_aliases = (
        ("sensitive", ("敏感肌", "敏感膚")),
        ("combination", ("混合肌", "混合性")),
        ("oily", ("油肌", "油性肌", "油性膚")),
        ("dry", ("乾肌", "乾性肌", "乾性膚")),
        ("normal", ("中性肌", "中性膚")),
    )
    for value, aliases in skin_aliases:
        if any(alias in text for alias in aliases):
            patch["skin_type"] = value
            break

    concerns: list[str] = []
    concern_aliases = (
        ("保濕", ("保濕", "乾燥", "缺水")),
        ("痘痘", ("抗痘", "痘痘", "粉刺")),
        ("美白", ("美白", "暗沉", "提亮")),
        ("斑點", ("淡斑", "斑點", "色素沉澱")),
        ("毛孔", ("毛孔",)),
        ("抗老", ("抗老", "細紋", "皺紋")),
        ("舒緩", ("舒緩", "泛紅", "敏感")),
    )
    for value, aliases in concern_aliases:
        if any(alias in text for alias in aliases):
            concerns.append(value)
    if concerns:
        patch["concern"] = list(dict.fromkeys(concerns))
    return patch


def _has_product_context(
    conversation_flow: str | None,
    current_step: str | None,
    structured_state: dict[str, Any] | None,
) -> bool:
    state = structured_state or {}
    flow = str(conversation_flow or state.get("active_flow") or "").lower()
    step = str(current_step or state.get("current_step") or "").lower()
    intent_type = str(state.get("intent_type") or "").lower()
    return bool(
        flow == "product_search"
        or step.startswith("product")
        or intent_type in _PRODUCT_INTENT_TYPES
        or state.get("has_products")
        or state.get("product_category") not in (None, "", "不明")
    )


def _result(
    *,
    route: ResolvedRoute,
    reason: str,
    action: str | None = None,
    parser: bool = False,
    inherit: bool = False,
    clarify: bool = False,
    patch: dict[str, Any] | None = None,
) -> ProductTurnResolution:
    return {
        "route": route,
        "action": action,
        "reason": reason,
        "requires_intent_parser": parser,
        "inherit_context": inherit,
        "needs_clarification": clarify,
        "intent_patch": patch or {},
    }


def resolve_product_turn(
    text: str,
    *,
    conversation_flow: str | None = None,
    current_step: str | None = None,
    structured_state: dict[str, Any] | None = None,
    action: str | None = None,
) -> ProductTurnResolution:
    """Resolve a product turn locally, or explicitly request the LLM parser."""
    normalized = _compact(text)
    flow = str(
        conversation_flow or (structured_state or {}).get("active_flow") or ""
    ).lower()
    if flow == "skin_record":
        return _result(route="unresolved", reason="skin_record_flow", parser=True)

    canonical_action = _ACTION_ALIASES.get(str(action or "").strip().lower())
    if canonical_action:
        return _result(
            route="product_search",
            action=canonical_action,
            reason="explicit_structured_action",
            inherit=True,
        )

    product_context = _has_product_context(
        conversation_flow, current_step, structured_state
    )
    patch = _intent_patch(normalized)
    category = patch.get("product_category")

    if product_context and normalized in {"取消", "不要了", "停止", "算了"}:
        return _result(
            route="product_search", action="cancel", reason="product_cancel", inherit=True
        )

    # A named new category is a constraint change, not a pagination action.
    if product_context and not category:
        if any(marker in normalized for marker in _REPLACE_MARKERS):
            return _result(
                route="product_search",
                action="replace_batch",
                reason="replace_batch_phrase",
                inherit=True,
            )
        if any(marker in normalized for marker in _MORE_MARKERS):
            return _result(
                route="product_search",
                action="more_products",
                reason="more_products_phrase",
                inherit=True,
            )

    shopping = any(marker in normalized for marker in _SHOPPING_MARKERS)
    knowledge = any(marker in normalized for marker in _KNOWLEDGE_MARKERS)
    refinement = bool(patch) or any(marker in normalized for marker in _REFINEMENT_MARKERS)
    vague_product_request = (
        normalized.startswith("有沒有") and normalized.endswith("的")
    ) or normalized in {"請推薦一個", "推薦一個", "有推薦嗎", "推薦東西"}

    if product_context and knowledge and not shopping:
        return _result(route="skin_consult", reason="product_knowledge_question")

    if product_context and (category or refinement or shopping or vague_product_request):
        return _result(
            route="product_search",
            reason="product_context_refinement",
            inherit=True,
            patch=patch,
        )

    if category and shopping:
        return _result(
            route="product_search", reason="explicit_product_request", patch=patch
        )

    if category and knowledge:
        return _result(route="skin_consult", reason="product_knowledge_question")

    if category:
        return _result(route="product_search", reason="category_entry", patch=patch)

    if shopping or vague_product_request:
        return _result(
            route="product_search",
            reason="vague_product_request",
            clarify=True,
        )

    return _result(route="unresolved", reason="needs_semantic_parser", parser=True)


__all__ = ["ProductTurnResolution", "resolve_product_turn"]
