"""Exercise the real pooled embedding adapter without network/graph writes."""
import contextlib
import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock

import neo4j
import pytest
from app.clients.embedding_client import EmbeddingClient


def make_tool(monkeypatch, *, model=None, endpoint=None, timeout=None, payload=None, error=None):
    for key, value in {'NEO4J_URI': 'neo4j://offline.invalid', 'NEO4J_USER': 'offline',
                       'NEO4J_PASSWORD': 'unused-test-placeholder',
                       'EMBEDDING_BASE_URL': 'http://127.0.0.1:8002/v1',
                       'EMBEDDING_DIMENSION': '1024'}.items():
        monkeypatch.setenv(key, value)
    for key, value in {'EMBEDDING_MODEL': model, 'EMBEDDING_API_URL': endpoint,
                       'EMBEDDING_TIMEOUT_SECONDS': timeout}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    driver = Mock()
    monkeypatch.setattr(neo4j.GraphDatabase, 'driver', Mock(return_value=driver))
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload if payload is not None else {'data': [{'embedding': [1.] + [0.] * 1023}]}
    session = Mock()
    session.post.side_effect = error
    session.post.return_value = response
    monkeypatch.setattr(EmbeddingClient, '_build_session', lambda self: session)
    source = Path(__file__).resolve().parents[1] / 'tools/search/skinconcern_tool.py'
    spec = importlib.util.spec_from_file_location('isolated_skin_concern_contract', source)
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
        tool = module.SkinConcernTool()
    return tool, session, driver


@pytest.mark.parametrize('configured,expected', [
    (None, 'bge-m3'), (' BAAI/bge-m3 ', 'BAAI/bge-m3'),
    ('explicit-embedding-model', 'explicit-embedding-model'),
])
def test_skin_retriever_uses_real_client_and_explicit_model(monkeypatch, configured, expected):
    tool, session, driver = make_tool(monkeypatch, model=configured, timeout='3.5')
    assert tool._get_embedding('公開測試：保濕') == [1.] + [0.] * 1023
    session.post.assert_called_once()
    call = session.post.call_args
    assert call.args == ('http://127.0.0.1:8002/v1/embeddings',)
    assert call.kwargs['json'] == {'model': expected, 'input': ['公開測試：保濕']}
    assert call.kwargs['allow_redirects'] is False
    assert 0 < call.kwargs['timeout'] <= 3.5
    driver.session.assert_not_called()


def test_explicit_endpoint_precedence_and_same_session_no_query_cache(monkeypatch):
    tool, session, _ = make_tool(monkeypatch, endpoint='http://127.0.0.1:9000/custom-embedding')
    assert isinstance(tool.embedding_client, EmbeddingClient)
    assert tool._get_embedding('保濕')
    assert tool._get_embedding('乾燥')
    assert session.post.call_count == 2
    assert all(call.args == ('http://127.0.0.1:9000/custom-embedding',)
               and 0 < call.kwargs['timeout'] <= 2.0
               and call.kwargs['allow_redirects'] is False for call in session.post.call_args_list)


@pytest.mark.parametrize('embedding', [[1.], [True] * 1024, [float('nan')] * 1024,
                                      [float('inf')] * 1024, ['1'] * 1024])
def test_invalid_embedding_is_rejected_before_neo4j(monkeypatch, embedding):
    tool, session, driver = make_tool(monkeypatch, payload={'data': [{'embedding': embedding}]})
    with contextlib.redirect_stdout(io.StringIO()):
        assert tool.search_concern('公開保濕測試') is None
    driver.session.assert_not_called()
    assert session.post.call_count == 1


def test_timeout_fails_once_without_logging_upstream_payload(monkeypatch):
    tool, session, driver = make_tool(monkeypatch, error=TimeoutError('private-upstream-marker'))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert tool.search_concern('公開保濕測試') is None
    assert session.post.call_count == 1
    assert 0 < session.post.call_args.kwargs['timeout'] <= 2.0
    assert 'private-upstream-marker' not in output.getvalue()
    driver.session.assert_not_called()


def test_empty_query_stays_off_network_and_graph(monkeypatch):
    tool, session, driver = make_tool(monkeypatch)
    with contextlib.redirect_stdout(io.StringIO()):
        assert tool.search_concern(' ') is None
    session.post.assert_not_called()
    driver.session.assert_not_called()
