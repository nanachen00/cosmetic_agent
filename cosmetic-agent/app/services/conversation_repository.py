"""Lightweight conversation reads that avoid constructing the root orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tools.getzep.zep_memory_adapter import ZepMemoryAdapter


class ConversationRepository:
    """Read conversation metadata/history directly from the memory adapter."""

    def __init__(
        self,
        memory_factory: Callable[..., Any] = ZepMemoryAdapter,
    ) -> None:
        self._memory_factory = memory_factory

    def _memory(self, *, user_id: str, conversation_id: str) -> Any:
        return self._memory_factory(
            user_id=user_id,
            conversation_id=conversation_id,
        )

    def list(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        memory = self._memory(user_id=user_id, conversation_id="default")
        return memory.list_conversations_page(limit=limit, offset=offset)

    def count(self, *, user_id: str) -> int:
        memory = self._memory(user_id=user_id, conversation_id="default")
        return memory.count_conversations()

    def history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        lastn: int,
    ) -> list[dict[str, Any]]:
        memory = self._memory(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return memory.get_history(lastn=lastn)
