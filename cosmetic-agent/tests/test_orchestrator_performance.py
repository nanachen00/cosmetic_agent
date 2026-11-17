from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

import agents.root.orchestrator as module
from agents.root.orchestrator import (
    ConversationState, RootAgentOrchestrator, _knowledge_delta_callback,
    _needs_summary_confirmation, _with_knowledge_stream,
)
from schemas.intent import IntentParseResult, IntentType


def test_stream_callback_is_isolated_between_pooled_threads_and_reset_on_error():
    barrier = Barrier(2)
    callbacks = [object(), object()]

    @_with_knowledge_stream
    def process(self, user_input, **kwargs):
        barrier.wait(timeout=3)
        assert _knowledge_delta_callback.get() is callbacks[user_input]
        if user_input == 1:
            raise RuntimeError('fixture')
        return 'ok'

    def work(index):
        try:
            process(None, index, on_delta=callbacks[index])
        except RuntimeError:
            pass
        assert _knowledge_delta_callback.get() is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, range(2)))


@pytest.mark.parametrize('kind', [IntentType.KNOWLEDGE_QUERY, IntentType.SYMPTOM_SOLVING])
def test_regular_knowledge_and_symptom_routes_receive_stream_callback(monkeypatch, kind):
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root._log = lambda *args: None
    root._memory_prompt = lambda: ''
    root.user_id, root.conversation_id = 'fixture', 'fixture'
    callback = lambda value: None
    captured = []
    monkeypatch.setattr(module, 'should_search_named_product', lambda intent: False)
    monkeypatch.setattr(module, 'knowledge_agent_run', lambda **kwargs: captured.append(kwargs) or {'reply': 'answer'})
    token = _knowledge_delta_callback.set(callback)
    try:
        assert root._dispatch(IntentParseResult(intent_type=kind, raw_query='fixture')) == 'answer'
        assert captured[0]['on_delta'] is callback
    finally:
        _knowledge_delta_callback.reset(token)


def test_unneeded_pain_gain_model_call_is_skipped_for_medium_question(monkeypatch):
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root.state = ConversationState()
    root._log = lambda *args: None
    root._intent_memory_context = lambda: ''
    root._routing_context = lambda: {}
    root._enrich_intent_from_memory = lambda intent: intent
    root.model = 'fixture'
    root._do_dispatch = lambda intent: {'stage': 'dispatch'}
    question = '請說明玻尿酸的保濕作用以及一般日常使用時的注意事項'
    assert 20 < len(question) <= 40
    intent = IntentParseResult(intent_type=IntentType.KNOWLEDGE_QUERY, raw_query=question,
        ambiguity_score=0., routing_confidence=1., task_completeness=1.)
    monkeypatch.setattr(module, 'parse_intent', lambda **kwargs: intent)
    monkeypatch.setattr(module, '_analyze_pain_gain', lambda query: pytest.fail('Unused LLM analysis'))
    assert root._handle_new_input(question)['stage'] == 'dispatch'


def test_required_summary_confirmation_keeps_the_original_gate():
    intent = IntentParseResult(intent_type=IntentType.SYMPTOM_SOLVING, raw_query='fixture',
        ambiguity_score=.2, routing_confidence=1., task_completeness=1.)
    assert _needs_summary_confirmation('字' * 41, intent)
    assert not _needs_summary_confirmation('字' * 40, intent)
    intent.routing_confidence = .4
    assert not _needs_summary_confirmation('字' * 41, intent)
    intent.intent_type = IntentType.FIND_PRODUCT
    intent.routing_confidence = 1.
    assert not _needs_summary_confirmation('字' * 41, intent)


def test_an_already_streamed_answer_is_not_rewritten():
    root = RootAgentOrchestrator.__new__(RootAgentOrchestrator)
    root.response_composer = SimpleNamespace(compose=lambda **kwargs: pytest.fail('Duplicate rewrite'))
    intent = IntentParseResult(intent_type=IntentType.KNOWLEDGE_QUERY, raw_query='fixture')
    token = _knowledge_delta_callback.set(lambda value: None)
    try:
        assert root._compose_agent_reply(intent, '原始有依據的回答') == '原始有依據的回答'
    finally:
        _knowledge_delta_callback.reset(token)
