"""Server-authoritative limits for a single chat conversation."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any, Iterable

from app.security.chat_guard import estimate_tokens


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ConversationLimitPolicy:
    max_user_messages: int = 40
    max_tokens: int = 30_000
    warning_percent: int = 80

    @classmethod
    def from_env(cls) -> "ConversationLimitPolicy":
        return cls(
            max_user_messages=_env_int(
                "CHAT_CONVERSATION_MAX_USER_MESSAGES",
                40,
                minimum=5,
                maximum=200,
            ),
            max_tokens=_env_int(
                "CHAT_CONVERSATION_MAX_TOKENS",
                30_000,
                minimum=2_000,
                maximum=200_000,
            ),
            warning_percent=_env_int(
                "CHAT_CONVERSATION_WARNING_PERCENT",
                80,
                minimum=50,
                maximum=95,
            ),
        )


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "").lower()
    return str(getattr(message, "role", "") or "").lower()


def conversation_limit_status(
    messages: Iterable[Any],
    *,
    policy: ConversationLimitPolicy,
    incoming_user_text: str = "",
    incoming_assistant_text: str = "",
) -> dict[str, Any]:
    history = list(messages)
    user_messages = sum(1 for message in history if _message_role(message) == "user")
    total_messages = sum(
        1 for message in history if _message_role(message) in {"user", "assistant"}
    )
    used_tokens = sum(estimate_tokens(_message_content(message)) for message in history)

    if incoming_user_text:
        user_messages += 1
        total_messages += 1
        used_tokens += estimate_tokens(incoming_user_text)
    if incoming_assistant_text:
        total_messages += 1
        used_tokens += estimate_tokens(incoming_assistant_text)

    message_warning_at = math.ceil(
        policy.max_user_messages * policy.warning_percent / 100
    )
    token_warning_at = math.ceil(policy.max_tokens * policy.warning_percent / 100)
    blocked = (
        user_messages >= policy.max_user_messages
        or used_tokens >= policy.max_tokens
    )
    warning = blocked or user_messages >= message_warning_at or used_tokens >= token_warning_at
    reason = (
        "messages"
        if user_messages >= policy.max_user_messages
        else "tokens" if used_tokens >= policy.max_tokens else None
    )

    return {
        "blocked": blocked,
        "warning": warning,
        "reason": reason,
        "user_messages": user_messages,
        "total_messages": total_messages,
        "used_tokens": used_tokens,
        "max_user_messages": policy.max_user_messages,
        "max_tokens": policy.max_tokens,
        "remaining_user_messages": max(0, policy.max_user_messages - user_messages),
        "remaining_tokens": max(0, policy.max_tokens - used_tokens),
        "warning_percent": policy.warning_percent,
        "message": (
            "對話內容已額滿，請開新對話。"
            if blocked
            else "對話內容即將額滿，建議開新對話。"
            if warning
            else None
        ),
    }
