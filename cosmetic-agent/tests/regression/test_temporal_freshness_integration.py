from pathlib import Path


def test_freshness_gate_runs_before_input_guard():
    source = Path("agents/root/orchestrator.py").read_text(encoding="utf-8")
    assert source.index("classify_freshness_query(user_input)") < source.index("guard = guard_input(user_input)")


def test_freshness_gate_is_reversible_by_environment_flag():
    source = Path("agents/root/orchestrator.py").read_text(encoding="utf-8")
    assert "TEMPORAL_FRESHNESS_GUARD_ENABLED" in source
    assert '"freshness_grounded" if grounded_reply else "freshness_guard"' in source


def test_all_chat_entrypoints_prepare_context_before_sync_agent_execution():
    source = Path("app/api/routes/chat.py").read_text(encoding="utf-8")
    assert source.count("await _load_freshness_context(req.message)") == 3
    assert source.count("freshness_context=freshness_context") == 4
    assert "await run_in_threadpool(run_agent, freshness_context)" in source


def test_all_external_freshness_domains_fail_closed_without_a_provider():
    source = Path("agents/root/orchestrator.py").read_text(encoding="utf-8")
    assert 'freshness_decision["domain"] == "weather"' not in source
    assert 'and freshness_decision["requires_external_data"]' in source
