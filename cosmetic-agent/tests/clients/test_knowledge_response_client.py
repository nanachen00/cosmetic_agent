"""Completion-state and cleanup checks, with no provider/network requests."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.clients.knowledge_response_client import KnowledgeResponseClient


class FakeStream:
    def __init__(self, events, close_error=None):
        self.events = events
        self.close_error = close_error
        self.closed = 0
        self.consumed = 0

    def __iter__(self):
        for event in self.events:
            self.consumed += 1
            yield event

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def client(response):
    sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
    return KnowledgeResponseClient(provider="terra", model="gpt-5.6-terra", max_output_tokens=384,
                                   reasoning_effort="none", sdk_client=sdk)


def generate(adapter, on_delta=None):
    return adapter.generate(system_prompt="fixture instructions", messages=[{"role": "user", "content": "fixture"}],
                            on_delta=on_delta)


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_nonstream_rejects_partial_text_without_exposing_provider_payload(status, capsys):
    adapter = client(SimpleNamespace(status=status, output_text="private-provider-payload",
                                     incomplete_details={"reason": "private-provider-payload"}))
    with pytest.raises(RuntimeError, match="did not complete") as error:
        generate(adapter)
    assert "private-provider-payload" not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_nonstream_completed_retains_whole_output_and_request_limits():
    adapter = client(SimpleNamespace(status="completed", output_text=" 第一段。\n第二段。 "))
    assert generate(adapter) == "第一段。\n第二段。"
    kwargs = adapter.sdk_client.responses.create.call_args.kwargs
    assert kwargs["max_output_tokens"] == 384
    assert kwargs["model"] == "gpt-5.6-terra"
    assert kwargs["store"] is False


def test_completed_stream_keeps_all_deltas_and_closes_once():
    stream = FakeStream([
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_text.delta", delta="第一段。"),
        SimpleNamespace(type="response.output_text.delta", delta="\n第二段。"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed")),
    ])
    adapter = client(stream)
    deltas = []
    assert generate(adapter, deltas.append) == "第一段。\n第二段。"
    assert deltas == ["第一段。", "\n第二段。"]
    assert stream.closed == 1
    assert adapter.sdk_client.responses.create.call_args.kwargs["stream"] is True


@pytest.mark.parametrize("event_type", ["response.incomplete", "response.failed", "response.cancelled",
                                       "response.error", "error"])
def test_failure_event_immediately_rejects_partial_and_cleanup_cannot_mask_it(event_type, capsys):
    stream = FakeStream([
        SimpleNamespace(type="response.output_text.delta", delta="partial"),
        SimpleNamespace(type=event_type, message="private-provider-payload"),
        SimpleNamespace(type="response.completed"),
    ], close_error=RuntimeError("private-close-payload"))
    with pytest.raises(RuntimeError, match="did not complete") as error:
        generate(client(stream), lambda delta: None)
    assert stream.consumed == 2
    assert stream.closed == 1
    assert "private" not in str(error.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("events", [[], [SimpleNamespace(type="response.output_text.delta", delta="partial")]])
def test_missing_terminal_event_rejects_even_nonempty_partial_text(events):
    stream = FakeStream(events)
    with pytest.raises(RuntimeError, match="without completion"):
        generate(client(stream), lambda delta: None)
    assert stream.closed == 1


def test_callback_exception_is_preserved_when_close_also_fails():
    stream = FakeStream([SimpleNamespace(type="response.output_text.delta", delta="first")],
                        close_error=RuntimeError("cleanup failure"))
    callback_error = ValueError("callback failure")
    def callback(delta):
        raise callback_error
    with pytest.raises(ValueError) as caught:
        generate(client(stream), callback)
    assert caught.value is callback_error
    assert stream.closed == 1


def test_false_completed_status_is_not_accepted():
    stream = FakeStream([SimpleNamespace(type="response.completed", response=SimpleNamespace(status="incomplete"))])
    with pytest.raises(RuntimeError, match="did not complete"):
        generate(client(stream), lambda delta: None)
    assert stream.closed == 1
