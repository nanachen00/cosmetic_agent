"""Automatic conversation titles from the first real user message.

RECONSTRUCTED 2026-10-21 from agents/root/orchestrator.py. Contract:
``maybe_title_conversation(adapter=..., first_message=..., flow=...) -> bool``.
It must run before the turn is written, never block chat on failure and never
overwrite a manual title.

Model path: the OpenAI-compatible endpoint in TITLE_MODEL_BASE_URL (defaults
to the local vLLM/TAIDE server). Short messages use a fast path without a model call.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)
_MAX_TITLE_CHARS = 16
_FAST_PATH_CHARS = 16
_SKIP_FLOWS = {"skin_tracking", "cycle_skin_tracking"}
_SKIP_MESSAGES = {
    "cycle_skin_tracking",
    "我要記錄膚況",
    "我要紀錄膚況",
    "我要記錄今天的膚況",
    "我要紀錄今天的膚況",
    "開始記錄膚況",
    "開始紀錄膚況",
}


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _sanitize_title(text: str) -> str:
    title = _clean(text).strip("「」『』\"'“”：:。.!！?？ ")
    title = re.sub(r"^(標題|title)\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    return title[:_MAX_TITLE_CHARS]


class ConversationTitleService:
    def __init__(self, *, client: Any | None = None, enabled: bool | None = None) -> None:
        self._client = client
        self.enabled = (
            os.getenv("CONVERSATION_TITLE_LLM", "1").strip().lower() in {"1", "true", "yes", "on"}
            if enabled is None
            else enabled
        )
        self.model = os.getenv("TITLE_MODEL", os.getenv("LOCAL_CHAT_MODEL", "taide-12b"))
        self.timeout = float(os.getenv("TITLE_TIMEOUT_SECONDS", "4"))

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=os.getenv(
                    "TITLE_MODEL_BASE_URL",
                    os.getenv("TAIDE_BASE_URL", "http://127.0.0.1:8000/v1"),
                ),
                api_key=os.getenv("LOCAL_LLM_API_KEY", "EMPTY"),
                timeout=self.timeout,
                max_retries=0,
            )
        return self._client

    @staticmethod
    def _current_entry(adapter: Any) -> dict | None:
        try:
            metadata = adapter._get_user_metadata()
        except Exception:
            return None
        for item in metadata.get("conversations", []) or []:
            if item.get("conversation_id") == getattr(adapter, "conversation_id", None):
                return item
        return None

    def _summarize(self, message: str) -> str:
        if len(message) <= _FAST_PATH_CHARS or not self.enabled:
            return _sanitize_title(message)
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是對話標題產生器。用繁體中文（台灣用語）為使用者的第一句話"
                        f"寫一個不超過 {_MAX_TITLE_CHARS} 個字的標題。只輸出標題本身，"
                        "不要加引號、標點或解釋。"
                    ),
                },
                {"role": "user", "content": message[:500]},
            ],
            max_tokens=32,
            temperature=0.2,
        )
        title = _sanitize_title(response.choices[0].message.content or "")
        return title or _sanitize_title(message)

    def maybe_title_conversation(self, *, adapter: Any, first_message: str, flow: str | None = None) -> bool:
        message = _clean(first_message)
        if not message or message in _SKIP_MESSAGES or (flow or "") in _SKIP_FLOWS:
            return False
        entry = self._current_entry(adapter)
        if entry is not None and (
            entry.get("has_user_message")
            or entry.get("title_source") == "manual"
            or (entry.get("title") or "新對話") != "新對話"
        ):
            return False

        try:
            title = self._summarize(message)
        except Exception as error:  # model outage must not break chat
            _LOGGER.warning("conversation title model failed: %s", type(error).__name__)
            title = _sanitize_title(message)
        if not title:
            return False

        # The adapter writes the pending title when the turn is committed.
        if hasattr(adapter, "_pending_title"):
            adapter._pending_title = title
        if entry is not None:
            try:
                metadata = adapter._get_user_metadata()
                for item in metadata.get("conversations", []) or []:
                    if item.get("conversation_id") == adapter.conversation_id:
                        if item.get("title_source") == "manual":
                            return False
                        item["title"] = title
                        item["title_source"] = "auto"
                adapter._save_user_metadata(metadata)
            except Exception as error:
                _LOGGER.warning("conversation title save failed: %s", type(error).__name__)
                return False
        return True
