from __future__ import annotations

import pytest

from app.clients.chat_completion_client import ChatCompletionClient, ChatCompletionError


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


def _client(session):
    return ChatCompletionClient(
        base_url="http://127.0.0.1:8000/v1/",
        default_timeout_seconds=30,
        session=session,
    )


def test_chat_client_uses_openai_contract_and_reuses_session():
    session = _Session(_Response({"choices": [{"message": {"content": "  完成  "}}]}))
    client = _client(session)
    messages = [{"role": "user", "content": "測試"}]

    assert client.complete(
        messages=messages,
        model="taide-12b",
        max_tokens=60,
        temperature=0,
        timeout_seconds=15,
    ) == "完成"
    assert client.complete(
        messages=messages,
        model="taide-12b",
        max_tokens=30,
    ) == "完成"

    assert session.calls[0] == (
        "http://127.0.0.1:8000/v1/chat/completions",
        {
            "model": "taide-12b",
            "messages": messages,
            "max_tokens": 60,
            "temperature": 0.0,
        },
        15.0,
    )
    assert session.calls[1][2] == 30.0


def test_chat_client_has_no_hidden_retry():
    session = _Session(error=TimeoutError("busy"))

    with pytest.raises(ChatCompletionError, match="busy"):
        _client(session).complete(
            messages=[{"role": "user", "content": "測試"}],
            model="taide-12b",
            max_tokens=20,
        )

    assert len(session.calls) == 1


def test_chat_client_rejects_empty_response():
    session = _Session(_Response({"choices": [{"message": {"content": " "}}]}))

    with pytest.raises(ChatCompletionError, match="no text content"):
        _client(session).complete(
            messages=[{"role": "user", "content": "測試"}],
            model="taide-12b",
            max_tokens=20,
        )
