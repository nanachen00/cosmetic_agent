"""Clients for services outside the unified FastAPI process."""

from .chat_completion_client import ChatCompletionClient, ChatCompletionError
from .embedding_client import EmbeddingClient, EmbeddingServiceError

__all__ = [
    "ChatCompletionClient",
    "ChatCompletionError",
    "EmbeddingClient",
    "EmbeddingServiceError",
]
