from __future__ import annotations

import pytest

from app.clients.embedding_client import EmbeddingClient, EmbeddingServiceError


class _Response:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if self.error:
            raise self.error
        return self.response


def _client(session, *, dimension=3):
    return EmbeddingClient(
        base_url="http://127.0.0.1:8002/v1/",
        model="bge-m3",
        timeout_seconds=2.0,
        dimension=dimension,
        session=session,
    )


def test_embedding_client_uses_expected_contract_and_reuses_session():
    session = _Session(_Response({"data": [{"embedding": [0, 0.5, 1]}]}))
    client = _client(session)

    assert client.embed_query("保濕精華") == [0.0, 0.5, 1.0]
    assert client.embed_query("敏感肌") == [0.0, 0.5, 1.0]
    assert session.calls == [
        (
            "http://127.0.0.1:8002/v1/embeddings",
            {"model": "bge-m3", "input": ["保濕精華"]},
            2.0,
        ),
        (
            "http://127.0.0.1:8002/v1/embeddings",
            {"model": "bge-m3", "input": ["敏感肌"]},
            2.0,
        ),
    ]


def test_embedding_client_rejects_invalid_dimension():
    session = _Session(_Response({"data": [{"embedding": [0.5]}]}))

    with pytest.raises(EmbeddingServiceError, match="3-d"):
        _client(session).embed_query("保濕")


def test_embedding_client_fails_once_without_hidden_retry():
    session = _Session(error=TimeoutError("offline"))

    with pytest.raises(EmbeddingServiceError, match="offline"):
        _client(session).embed_query("保濕")

    assert len(session.calls) == 1


def test_embedding_client_rejects_empty_query_before_network():
    session = _Session(_Response({"data": [{"embedding": [0, 0, 0]}]}))

    with pytest.raises(ValueError, match="non-empty"):
        _client(session).embed_query("   ")

    assert session.calls == []
