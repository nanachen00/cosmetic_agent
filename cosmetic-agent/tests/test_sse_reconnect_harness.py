from tests.load.sse_reconnect import decode_sse, validate_target


def test_decode_sse_json_event():
    assert decode_sse([
        "id: opaque-7",
        "event: delta",
        'data: {"text":"hello"}',
    ]) == {
        "id": "opaque-7",
        "event": "delta",
        "data": {"text": "hello"},
    }


def test_decode_sse_multiline_and_comment():
    event = decode_sse([": keepalive", "event: message", "data: a", "data: b"])
    assert event == {"id": "", "event": "message", "data": "a\nb"}
    assert decode_sse([": keepalive"]) is None


def test_target_guard_accepts_staging_and_rejects_production_port():
    assert validate_target("http://127.0.0.1:18110") == "http://127.0.0.1:18110"
    assert validate_target("https://staging-api.blinkai.tw/") == "https://staging-api.blinkai.tw"
    try:
        validate_target("http://127.0.0.1:8010")
    except ValueError as exc:
        assert "production port" in str(exc)
    else:
        raise AssertionError("production port must be rejected")
