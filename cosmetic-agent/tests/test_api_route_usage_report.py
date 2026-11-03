from datetime import datetime, timezone
from pathlib import Path

from scripts.report_api_route_usage import iter_http_events, summarize


def test_route_usage_ignores_non_json_and_non_http_events(tmp_path: Path) -> None:
    log = tmp_path / "backend-8010.log"
    log.write_text(
        "not-json\n"
        '{"event":"startup"}\n'
        '{"event":"http_request","timestamp":"2026-09-25T10:00:00+00:00",'
        '"http_method":"GET","http_route":"/readyz","http_status":200,"duration_ms":12.5}\n',
        encoding="utf-8",
    )
    events = list(iter_http_events([log], cutoff=datetime(2026, 9, 25, tzinfo=timezone.utc)))
    assert len(events) == 1
    assert events[0]["http_route"] == "/readyz"


def test_route_usage_groups_normalized_routes_and_calculates_p95(tmp_path: Path) -> None:
    log = tmp_path / "backend-8010.log"
    log.write_text(
        "\n".join([
            '{"event":"http_request","timestamp":"2026-09-25T10:00:00Z","http_method":"get","http_route":"/api/users/{user_id}","http_status":200,"duration_ms":10}',
            '{"event":"http_request","timestamp":"2026-09-25T10:01:00Z","http_method":"GET","http_route":"/api/users/{user_id}","http_status":503,"duration_ms":40}',
        ]) + "\n",
        encoding="utf-8",
    )
    rows = summarize(iter_http_events([log]))
    assert rows == [{
        "method": "GET",
        "route": "/api/users/{user_id}",
        "requests": 2,
        "server_errors": 1,
        "statuses": {"200": 1, "503": 1},
        "p95_ms": 40.0,
        "last_seen": "2026-09-25T10:01:00+00:00",
    }]
