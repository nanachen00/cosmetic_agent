"""Natural-tone response composer with fact-preservation guards.

RECONSTRUCTED 2026-10-21 from test/test_response_composer.py and
agents/root/orchestrator.py::_compose_agent_reply.

The composer may rephrase an agent reply, but the original reply is returned
whenever the rewrite drops a protected fact (product/brand name, price, safety
warning), changes product order, adds a number, grows far beyond the original,
or the model fails.

Hotfix 2026-10-29 (post-dispatch review L4/O1): system notices such as
"知識庫暫時維護中" are never sent to the model, and knowledge/symptom replies
(already written by the knowledge agent) skip the rewrite by default.
RESPONSE_COMPOSER_SKIP_INTENTS overrides that list; set it to "" to restore
the previous behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence

_LOGGER = logging.getLogger(__name__)

_PRICE_PATTERN = re.compile(r"(?:NT\$|NTD\s?|＄|\$)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?元")
_SAFETY_TERMS = (
    "停用",
    "就醫",
    "醫師",
    "醫生",
    "皮膚科",
    "孕婦",
    "懷孕",
    "哺乳",
    "禁用",
    "不建議",
    "過敏",
)
_MAX_CONTEXT_TURNS = 6
_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*")
# Fallback/error replies carry no facts to rephrase; a rewrite can only invent some.
_SYSTEM_NOTICE_PATTERN = re.compile(r"請稍後再試|發生錯誤|暫時無法|維護中|無法取得|找不到|不太理解")
_SYSTEM_NOTICE_MAX_CHARS = 120
_DEFAULT_SKIP_INTENTS = ("knowledge_query", "symptom_solving")
_MAX_EXPANSION_RATIO = 2.0
_MAX_EXPANSION_SLACK_CHARS = 80

INSTRUCTIONS = """你是 BLINK AI 美妝保養助理的回覆潤飾器。
輸入是 JSON：user_query、intent_type、raw_reply（系統已查證的原始回覆）、
conversation_context（最近對話）、protected_terms（必須逐字保留的商品／品牌名稱）。
請用繁體中文（台灣用語）把 raw_reply 改寫得自然、口語、貼近使用者剛才的需求：
- 不新增 raw_reply 沒有的事實、成分、功效、價格或商品。
- protected_terms、所有價格與數字必須逐字保留，商品順序不可改變。
- 安全提醒（停用、就醫、過敏、孕期等）必須保留。
- 不做醫療診斷。
只輸出 JSON：{"reply": "改寫後的回覆"}"""


@dataclass(frozen=True)
class ComposedReply:
    reply: str
    used_model: bool
    fallback_reason: str | None = None


def _env_enabled() -> bool:
    return os.getenv("RESPONSE_COMPOSER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def _env_skip_intents() -> frozenset[str]:
    raw = os.getenv("RESPONSE_COMPOSER_SKIP_INTENTS")
    values = _DEFAULT_SKIP_INTENTS if raw is None else raw.split(",")
    return frozenset(value.strip().lower() for value in values if value.strip())


def _is_system_notice(raw_reply: str) -> bool:
    text = raw_reply.strip()
    return len(text) <= _SYSTEM_NOTICE_MAX_CHARS and bool(_SYSTEM_NOTICE_PATTERN.search(text))


def _first_positions(text: str, terms: Sequence[str]) -> list[tuple[int, str]]:
    return sorted((text.find(term), term) for term in terms if term and term in text)


class ResponseComposer:
    def __init__(
        self,
        *,
        client: Any | None = None,
        enabled: bool | None = None,
        model: str | None = None,
        skip_intents: Sequence[str] | None = None,
    ) -> None:
        self._client = client
        self.enabled = _env_enabled() if enabled is None else enabled
        self.skip_intents = (
            _env_skip_intents()
            if skip_intents is None
            else frozenset(str(value).strip().lower() for value in skip_intents if str(value).strip())
        )
        self.model = model or os.getenv(
            "RESPONSE_COMPOSER_MODEL", os.getenv("TERRA_MODEL", "gpt-5.6-terra")
        )

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                timeout=float(os.getenv("RESPONSE_COMPOSER_TIMEOUT_SECONDS", "8")),
                max_retries=0,
            )
        return self._client

    # ── guards ─────────────────────────────────────────────────────
    @staticmethod
    def _protected_facts(raw_reply: str, protected_terms: Sequence[str]) -> list[str]:
        facts: list[str] = [term for term in protected_terms if term and term in raw_reply]
        facts += [match.group(0).strip() for match in _PRICE_PATTERN.finditer(raw_reply)]
        facts += [term for term in _SAFETY_TERMS if term in raw_reply]
        return list(dict.fromkeys(facts))

    def _validate(self, raw_reply: str, candidate: str, protected_terms: Sequence[str]) -> str | None:
        normalized_candidate = candidate.replace(" ", "")
        for fact in self._protected_facts(raw_reply, protected_terms):
            if fact not in candidate and fact.replace(" ", "") not in normalized_candidate:
                return f"missing_protected_fact:{fact}"
        present = [term for term in protected_terms if term and term in raw_reply]
        raw_order = [term for _, term in _first_positions(raw_reply, present)]
        candidate_order = [term for _, term in _first_positions(candidate, present)]
        if raw_order != candidate_order:
            return "protected_order_changed"
        raw_numbers = set(_NUMBER_PATTERN.findall(raw_reply.replace(" ", "")))
        for number in _NUMBER_PATTERN.findall(normalized_candidate):
            if number not in raw_numbers:
                return f"added_number:{number}"
        raw_length = len(raw_reply.strip())
        if len(candidate) > raw_length * _MAX_EXPANSION_RATIO + _MAX_EXPANSION_SLACK_CHARS:
            return "expanded_beyond_raw"
        return None

    # ── public API ─────────────────────────────────────────────────
    def compose(
        self,
        *,
        user_query: str,
        intent_type: str,
        raw_reply: str,
        conversation_context: Sequence[dict] | None = None,
        protected_terms: Sequence[str] | None = None,
    ) -> ComposedReply:
        raw_reply = raw_reply or ""
        terms = [str(term).strip() for term in (protected_terms or []) if str(term).strip()]
        if not self.enabled:
            return ComposedReply(raw_reply, False, "disabled")
        if not raw_reply.strip():
            return ComposedReply(raw_reply, False, "empty_raw_reply")
        if _is_system_notice(raw_reply):
            return ComposedReply(raw_reply, False, "system_notice")
        if str(intent_type or "").strip().lower() in self.skip_intents:
            return ComposedReply(raw_reply, False, f"skipped_intent:{intent_type}")

        payload = {
            "user_query": user_query or "",
            "intent_type": intent_type or "",
            "raw_reply": raw_reply,
            "conversation_context": [
                {"role": str(turn.get("role", "")), "content": str(turn.get("content", ""))}
                for turn in list(conversation_context or [])[-_MAX_CONTEXT_TURNS:]
                if isinstance(turn, dict)
            ],
            "protected_terms": terms,
        }
        try:
            response = self._get_client().responses.create(
                model=self.model,
                instructions=INSTRUCTIONS,
                input=json.dumps(payload, ensure_ascii=False),
                store=False,
            )
            text = getattr(response, "output_text", "") or ""
            candidate = str(json.loads(text).get("reply") or "").strip()
        except json.JSONDecodeError:
            return ComposedReply(raw_reply, False, "invalid_model_output")
        except Exception as error:
            _LOGGER.warning("response composer failed: %s", type(error).__name__)
            return ComposedReply(raw_reply, False, f"model_error:{type(error).__name__}")

        if not candidate:
            return ComposedReply(raw_reply, False, "empty_model_reply")
        reason = self._validate(raw_reply, candidate, terms)
        if reason:
            return ComposedReply(raw_reply, False, reason)
        return ComposedReply(candidate, True, None)
