from core.conversation_state import ConversationStateStore, shared_conversation_state
from schemas.intent import IntentParseResult


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        assert ttl == 1800
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class State:
    def __init__(self):
        self.intent = None
        self.seen_product_ids = set()
        self.turn_count = 0
        self.is_waiting_confirm = False
        self.skin_session = None


class Agent:
    def __init__(self, store, user="u", conversation="c"):
        self.state = State()
        self._conversation_state_store = store
        self.user_id, self.conversation_id = user, conversation

    @shared_conversation_state
    def process(self):
        self.state.turn_count += 1
        self.state.is_waiting_confirm = True
        return self.state.turn_count


def test_second_worker_loads_latest_state_and_expiration_drops_cached_state():
    redis = MemoryRedis()
    store = ConversationStateStore(redis)
    a, b = Agent(store), Agent(store)
    assert a.process() == 1
    assert b.process() == 2
    assert a.process() == 3
    assert Agent(store, conversation="different").process() == 1
    store.delete("u", "c")
    assert a.process() == 1


def test_round_trip_intent_sets_and_skin_dialogue_without_pickle():
    store = ConversationStateStore(MemoryRedis())
    state = State()
    state.intent = IntentParseResult(intent_type="knowledge_query", raw_query="保濕")
    state.seen_product_ids = {"product-b", "product-a"}
    state.skin_session = {"phase": "confirm", "observations": [{"zone": "left_cheek"}]}
    state.is_waiting_confirm = True
    store.save("user-1", "conversation-1", state)
    loaded = store.load("user-1", "conversation-1", State())
    assert isinstance(loaded.intent, IntentParseResult)
    assert loaded.intent.model_dump() == state.intent.model_dump()
    assert loaded.seen_product_ids == state.seen_product_ids
    assert loaded.skin_session == state.skin_session
    assert loaded.is_waiting_confirm
    assert "user-1" not in store.key("user-1", "conversation-1")


def test_root_stage_log_omits_user_text_and_memory(monkeypatch):
    from agents.root import orchestrator
    events = []
    monkeypatch.setattr(orchestrator, "log_event", lambda **fields: events.append(fields))
    agent = object.__new__(orchestrator.RootAgentOrchestrator)
    agent._log("INPUT", "private-user-input")
    agent._log("MEMORY", "private-memory-summary")
    assert [event["stage"] for event in events] == ["INPUT", "MEMORY"]
    assert all("private-" not in str(event) for event in events)
