"""Short-lived, JSON-only dialogue state shared between backend workers."""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache, wraps
import hashlib
import json
import os


def _json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported state type: {type(value).__name__}")


class ConversationStateStore:
    def __init__(self, client, *, prefix="blink:dialogue:v1", ttl=1800):
        self.client = client
        self.prefix = prefix
        self.ttl = ttl

    def key(self, user, conversation):
        identity = json.dumps([user, conversation], ensure_ascii=False).encode()
        return self.prefix + ":" + hashlib.sha256(identity).hexdigest()

    def load(self, user, conversation, template):
        raw = self.client.get(self.key(user, conversation))
        if raw is None:
            return type(template)()
        payload = json.loads(raw)
        if payload.get("version") != 1 or not isinstance(payload.get("state"), dict):
            raise ValueError("Invalid shared conversation state")
        restored = type(template)()
        allowed = vars(restored)
        for name, value in payload["state"].items():
            if name not in allowed:
                continue
            if name == "intent" and value is not None:
                from schemas.intent import IntentParseResult
                value = IntentParseResult.model_validate(value)
            elif isinstance(allowed[name], set):
                value = set(value)
            setattr(restored, name, value)
        return restored

    def save(self, user, conversation, state):
        payload = json.dumps({"version": 1, "state": vars(state)}, default=_json_value, ensure_ascii=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ValueError("Shared conversation state exceeds 1 MiB")
        self.client.setex(self.key(user, conversation), self.ttl, payload)

    def delete(self, user, conversation):
        self.client.delete(self.key(user, conversation))


@lru_cache(maxsize=4)
def _shared_store(url, prefix, ttl):
    from redis import Redis
    return ConversationStateStore(Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        max_connections=32,
    ), prefix=prefix, ttl=ttl)


def conversation_state_store_from_env():
    url = os.getenv("REDIS_URL", "").strip()
    required = os.getenv("CHAT_STATE_REQUIRE_REDIS", "0") == "1"
    if not url:
        if required:
            raise RuntimeError("CHAT_STATE_REQUIRE_REDIS=1 requires REDIS_URL")
        return None
    return _shared_store(url, os.getenv("CHAT_STATE_KEY_PREFIX", "blink:dialogue:v1"),
                         max(60, min(86400, int(os.getenv("CHAT_STATE_TTL_SECONDS", "1800")))))


def shared_conversation_state(method):
    """Caller must hold the existing per-account ChatGuard lock for this turn."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        store = getattr(self, "_conversation_state_store", None)
        if store is None:
            return method(self, *args, **kwargs)
        self.state = store.load(self.user_id, self.conversation_id, self.state)
        result = method(self, *args, **kwargs)
        store.save(self.user_id, self.conversation_id, self.state)
        return result
    return wrapped
