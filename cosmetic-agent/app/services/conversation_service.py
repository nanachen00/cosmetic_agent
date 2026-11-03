"""Conversation business operations shared by HTTP route adapters."""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping
from typing import Any
from uuid import uuid4


class ConversationQuotaExceeded(RuntimeError):
    """Raised when a user already has the configured number of conversations."""

    def __init__(self, *, current: int, maximum: int) -> None:
        super().__init__("conversation quota exceeded")
        self.current = current
        self.maximum = maximum


class ConversationService:
    """Own conversation memory operations without depending on FastAPI."""

    def __init__(
        self,
        *,
        agent_provider: Callable[..., Any],
        normalize_conversation_id: Callable[[str], str],
        sessions: MutableMapping[tuple[str, str], Any],
        conversation_limit_status: Callable[..., dict[str, Any]],
        conversation_limit_policy: Any,
        repository: Any | None = None,
    ) -> None:
        self._agent_provider = agent_provider
        self._normalize_conversation_id = normalize_conversation_id
        self._sessions = sessions
        self._conversation_limit_status = conversation_limit_status
        self._conversation_limit_policy = conversation_limit_policy
        self._repository = repository

    @property
    def max_conversations_per_user(self) -> int:
        """Visible conversation quota; the Zep index has the same default cap."""
        try:
            configured = int(os.getenv("CONVERSATION_MAX_PER_USER", "100"))
        except ValueError:
            configured = 100
        return max(20, min(configured, 100))

    def list(self, *, user_id: str) -> list[dict[str, Any]]:
        if self._repository is not None:
            conversations, _ = self._repository.list(
                user_id=user_id,
                limit=self.max_conversations_per_user,
                offset=0,
            )
            return conversations
        agent = self._agent_provider(user_id=user_id, conversation_id="default")
        return agent.memory.list_conversations()

    def list_page(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if self._repository is not None:
            return self._repository.list(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
        agent = self._agent_provider(user_id=user_id, conversation_id="default")
        memory = agent.memory
        page_reader = getattr(memory, "list_conversations_page", None)
        if callable(page_reader):
            return page_reader(limit=limit, offset=offset)
        conversations = memory.list_conversations()
        return conversations[offset : offset + limit], len(conversations)

    def conversation_count(self, *, user_id: str) -> int:
        if self._repository is not None:
            return self._repository.count(user_id=user_id)
        agent = self._agent_provider(user_id=user_id, conversation_id="default")
        memory = agent.memory
        counter = getattr(memory, "count_conversations", None)
        if callable(counter):
            return counter()
        return len(memory.list_conversations())

    @property
    def quota_enforcement_mode(self) -> str:
        """Describe the current storage guarantee without overstating it.

        Zep stores the conversation index in user metadata and exposes no atomic
        compare-and-set operation. The create check therefore blocks ordinary
        over-limit requests, but concurrent workers can still race until a
        transactional reservation is moved to PostgreSQL or Redis.
        """
        return "best_effort_non_atomic"

    def create_draft(self, *, user_id: str) -> dict[str, str]:
        current = self.conversation_count(user_id=user_id)
        maximum = self.max_conversations_per_user
        if current >= maximum:
            raise ConversationQuotaExceeded(current=current, maximum=maximum)
        return {"conversation_id": uuid4().hex, "title": "新對話"}

    def rename(self, *, user_id: str, conversation_id: str, title: str) -> dict[str, Any]:
        normalized_id = self._normalize_conversation_id(conversation_id)
        agent = self._agent_provider(user_id=user_id, conversation_id=normalized_id)
        return agent.memory.rename_conversation(conversation_id, title)

    def delete(self, *, user_id: str, conversation_id: str) -> str:
        normalized_id = self._normalize_conversation_id(conversation_id)
        agent = self._agent_provider(user_id=user_id, conversation_id=normalized_id)
        agent.memory.delete_conversation(normalized_id)
        self._sessions.pop((user_id, normalized_id), None)
        if os.getenv("AUTH_STORAGE_BACKEND", "postgres").strip().lower() == "postgres":
            from database.postgres.repositories.conversation import (
                postgres_conversation_repository,
            )

            postgres_conversation_repository.delete(
                user_id=user_id,
                conversation_id=normalized_id,
            )
        return normalized_id

    def history(
        self,
        *,
        user_id: str,
        conversation_id: str,
        lastn: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._repository is not None:
            messages = self._repository.history(
                user_id=user_id,
                conversation_id=conversation_id,
                lastn=lastn,
            )
        else:
            agent = self._agent_provider(
                user_id=user_id,
                conversation_id=conversation_id,
            )
            messages = agent.memory.get_history(lastn=lastn)
        limit = self._conversation_limit_status(
            messages,
            policy=self._conversation_limit_policy,
        )
        return messages, limit
