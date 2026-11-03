"""Application service for producing the existing welcome response."""

from collections.abc import Callable
from typing import Any, Protocol


class WelcomeAgent(Protocol):
    def get_welcome(self) -> Any: ...


AgentFactory = Callable[[str], WelcomeAgent]


class WelcomeService:
    def __init__(self, agent_factory: AgentFactory) -> None:
        self._agent_factory = agent_factory

    def get_welcome(self, owner_id: str) -> Any:
        return self._agent_factory(owner_id).get_welcome()
