from __future__ import annotations

import pytest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.clients.embedding_client import EmbeddingClient, EmbeddingServiceError


class _Response:
    def __init__(self, payload, error=None, status=200):
        self._payload = payload
        self._error = error
        self.status_code = status

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

    def post(self, url, *, json, timeout, allow_redirects):
        assert allow_redirects is False
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
    assert [(url, payload) for url, payload, _ in session.calls] == [
        (
            "http://127.0.0.1:8002/v1/embeddings",
            {"model": "bge-m3", "input": ["保濕精華"]},
        ),
        (
            "http://127.0.0.1:8002/v1/embeddings",
            {"model": "bge-m3", "input": ["敏感肌"]},
        ),
    ]
    assert all(0 < timeout <= 2.0 for _, _, timeout in session.calls)


def test_embedding_client_rejects_invalid_dimension():
    session = _Session(_Response({"data": [{"embedding": [0.5]}]}))

    with pytest.raises(EmbeddingServiceError, match="3-d"):
        _client(session).embed_query("保濕")


def test_embedding_client_fails_once_without_hidden_retry():
    session = _Session(error=TimeoutError("offline"))

    with pytest.raises(EmbeddingServiceError, match="service request failed"):
        _client(session).embed_query("保濕")

    assert len(session.calls) == 1


def test_embedding_client_rejects_empty_query_before_network():
    session = _Session(_Response({"data": [{"embedding": [0, 0, 0]}]}))

    with pytest.raises(ValueError, match="non-empty"):
        _client(session).embed_query("   ")

    assert session.calls == []


@pytest.mark.parametrize("vector", [[float("nan"), 1, 2], [float("inf"), 1, 2],
    [0, 0, 0], [True, 1, 2], [1.7e308, 1.7e308, 1.7e308], ["1", 1, 2]])
def test_invalid_vectors_cannot_enter_retrieval_and_do_not_leak_slots(vector):
    session = _Session(_Response({"data": [{"embedding": vector}]}))
    client = _client(session)
    for _ in range(client.pool_maxsize + 1):
        with pytest.raises(EmbeddingServiceError):
            client.embed_query("private skin concern")
    session.response = _Response({"data": [{"embedding": [1, 0, 0]}]})
    assert client.embed_query("next query") == [1., 0., 0.]


def test_malformed_payload_error_does_not_expose_upstream_text_or_credentials():
    session = _Session(error=RuntimeError("private skin concern https://user:secret@host"))
    client = _client(session)
    with pytest.raises(EmbeddingServiceError) as error:
        client.embed_query("private skin concern")
    assert str(error.value) == "embedding service request failed"
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("payload", [{"data": [None]}, {"data": "bad"}, None, []])
def test_malformed_vector_response_is_rejected(payload):
    with pytest.raises(EmbeddingServiceError, match="3-d"):
        _client(_Session(_Response(payload))).embed_query("query")


def test_redirect_response_is_not_accepted_or_retried():
    session = _Session(_Response({"data": [{"embedding": [1, 0, 0]}]}, status=307))
    with pytest.raises(EmbeddingServiceError, match="unexpected status"):
        _client(session).embed_query("query")
    assert len(session.calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_configuration_must_be_finite(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        EmbeddingClient(base_url="http://127.0.0.1:8002/v1", model="bge-m3", timeout_seconds=timeout)


def test_saturated_pool_wait_is_bounded_without_a_second_network_request():
    entered, release = threading.Event(), threading.Event()
    class BlockingSession(_Session):
        def post(self, *args, **kwargs):
            result = super().post(*args, **kwargs)
            entered.set()
            assert release.wait(2)
            return result
    session = BlockingSession(_Response({"data": [{"embedding": [1, 0, 0]}]}))
    client = EmbeddingClient(base_url="http://127.0.0.1:8002/v1", model="bge-m3",
        timeout_seconds=.05, pool_maxsize=1, dimension=3, session=session)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.embed_query, "first")
        try:
            assert entered.wait(1)
            start = time.monotonic()
            with pytest.raises(EmbeddingServiceError, match="capacity unavailable"):
                client.embed_query("second")
            assert time.monotonic() - start < .5
            assert len(session.calls) == 1
        finally:
            release.set()
        assert first.result(timeout=1) == [1., 0., 0.]
    assert client.embed_query("after release") == [1., 0., 0.]


def test_pool_wait_is_deducted_from_remaining_socket_timeout():
    session = _Session(_Response({"data": [{"embedding": [1, 0, 0]}]}))
    client = _client(session)
    with patch("app.clients.embedding_client.time.monotonic", side_effect=[10., 11.5]):
        client.embed_query("query")
    assert session.calls[0][2] == .5
