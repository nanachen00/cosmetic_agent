"""In-memory conversation history for the Knowledge answer flow."""


class KnowledgeConversationHistory:
    """Keep per-session turns without coupling storage rules to the agent."""

    def __init__(self, max_history_turns: int = 20):
        self.max_history_turns = max_history_turns
        self.conversations: dict[str, list[dict[str, str]]] = {}

    def get(self, session_id: str) -> list[dict[str, str]]:
        if session_id not in self.conversations:
            self.conversations[session_id] = []
        return self.conversations[session_id]

    def append(self, session_id: str, role: str, content: str) -> None:
        history = self.get(session_id)
        history.append({"role": role, "content": content})

        max_len = self.max_history_turns * 2
        if len(history) > max_len:
            self.conversations[session_id] = history[-max_len:]

    def clear(self, session_id: str) -> None:
        self.conversations.pop(session_id, None)

    def last_user_query(self, session_id: str) -> str:
        for message in reversed(self.get(session_id)):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""
