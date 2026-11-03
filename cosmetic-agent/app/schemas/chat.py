from typing import Optional

from pydantic import BaseModel, Field


def normalize_conversation_id(conversation_id: Optional[str]) -> str:
    value = (conversation_id or "default").strip()
    return value or "default"


class ChatRequest(BaseModel):
    __module__ = "api.api_server"

    user_id: Optional[str] = Field(default=None, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)
    conversation_id: Optional[str] = Field(default="default", max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    flow: Optional[str] = Field(default=None, max_length=64)
    action: Optional[str] = Field(default=None, max_length=64)

    @property
    def uid(self) -> str:
        return self.user_id or self.session_id or "anonymous"

    @property
    def cid(self) -> str:
        return normalize_conversation_id(self.conversation_id)

