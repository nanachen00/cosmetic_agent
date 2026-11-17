from __future__ import annotations

from contextlib import contextmanager

from tools.user import user_analysis_context_bridge as bridge_module
from tools.user.user_analysis_context_bridge import UserAnalysisContextBridge


USER_ID = "11111111-1111-4111-8111-111111111111"


class RecordingCursor:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params) -> None:
        statement = " ".join(str(query).split())
        self.queries.append(statement)
        if "FROM users u" in statement:
            self.row = {
                "profile_data": {},
                "preferred_categories": [],
                "disliked_ingredients": [],
                "budget_min": None,
                "budget_max": 1200,
                "preferences_data": {"preferred_textures": ["light"]},
                "total_event_count": 4,
            }
        elif "FROM quiz_attempts" in statement:
            self.row = {}
        elif "RETURNING total_event_count" in statement:
            self.row = {"total_event_count": 5}
        else:
            self.row = None

    def fetchone(self):
        return self.row


class RecordingConnection:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    def cursor(self):
        return RecordingCursor(self.queries)


def _install_recording_database(monkeypatch):
    queries: list[str] = []

    @contextmanager
    def fake_postgres():
        yield RecordingConnection(queries)

    monkeypatch.setattr(bridge_module, "postgres_connection", fake_postgres)
    return queries


def test_turn_load_is_two_reads_with_no_count_or_snapshot(monkeypatch):
    queries = _install_recording_database(monkeypatch)
    bridge = UserAnalysisContextBridge(USER_ID)

    context = bridge.load()

    assert context["budget_max"] == 1200
    assert context["preferred_textures"] == ["light"]
    assert context["total_event_count"] == 4
    assert len(queries) == 2
    assert not any("COUNT(*)" in query for query in queries)
    assert not any("INSERT INTO user_analysis_profiles" in query for query in queries)


def test_compat_refresh_is_read_only_until_snapshot_is_requested(monkeypatch):
    queries = _install_recording_database(monkeypatch)
    bridge = UserAnalysisContextBridge(USER_ID)

    bridge.refresh()
    assert len(queries) == 2

    context = bridge.refresh(analyze_if_needed=True)
    assert context["total_event_count"] == 5
    assert len(queries) == 5
    snapshot_queries = [
        query for query in queries if "INSERT INTO user_analysis_profiles" in query
    ]
    assert len(snapshot_queries) == 1
    assert "WITH event_total AS" in snapshot_queries[0]
    assert "COUNT(*)" in snapshot_queries[0]


def test_record_and_deliberate_snapshot_use_one_statement_each(monkeypatch):
    queries = _install_recording_database(monkeypatch)
    bridge = UserAnalysisContextBridge(USER_ID, "conversation-1")

    assert bridge.record_intent({"intent_type": "find_product"}, "保濕精華")
    snapshot = bridge.persist_snapshot({"preferred_textures": ["light"]})

    assert snapshot["total_event_count"] == 5
    assert len(queries) == 2
    assert "INSERT INTO user_behavior_events" in queries[0]
    assert "INSERT INTO user_analysis_profiles" in queries[1]

