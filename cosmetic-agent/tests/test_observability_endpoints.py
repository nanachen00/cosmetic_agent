import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prometheus_client import CollectorRegistry, generate_latest, multiprocess

from core import observability
from core.observability import install_observability


def _build_app(*, ready: bool = True) -> FastAPI:
    app = FastAPI()
    install_observability(
        app,
        readiness_checks={"dependency": lambda: ready},
    )
    return app


def test_livez_and_readyz_include_release(monkeypatch):
    monkeypatch.setenv("APP_RELEASE", "staging-test-release")
    with TestClient(_build_app()) as client:
        live = client.get("/livez")
        ready = client.get("/readyz")

    assert live.status_code == 200
    assert live.headers["x-app-release"] == "staging-test-release"
    assert ready.status_code == 200
    assert ready.json()["release"] == "staging-test-release"


def test_readyz_returns_503_for_failed_dependency():
    with TestClient(_build_app(ready=False)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["dependencies"]["dependency"] == "unavailable"


def test_metrics_is_hidden_from_non_loopback_client(monkeypatch):
    monkeypatch.delenv("METRICS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("METRICS_ALLOW_LOOPBACK", "1")
    with TestClient(_build_app()) as client:
        response = client.get("/metrics")

    assert response.status_code == 404


def test_metrics_accepts_configured_bearer_token(monkeypatch):
    monkeypatch.setenv("METRICS_BEARER_TOKEN", "test-metrics-token")
    with TestClient(_build_app()) as client:
        response = client.get(
            "/metrics",
            headers={"Authorization": "Bearer test-metrics-token"},
        )

    assert response.status_code == 200
    assert "blink_http_requests_total" in response.text



def test_metrics_payload_uses_multiprocess_collector(monkeypatch, tmp_path):
    registry = object()
    collected = []
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(observability, "CollectorRegistry", lambda: registry)
    monkeypatch.setattr(
        observability.multiprocess,
        "MultiProcessCollector",
        lambda value: collected.append(value),
    )
    monkeypatch.setattr(
        observability,
        "generate_latest",
        lambda value=None: b"multiprocess" if value is registry else b"default",
    )

    assert observability._generate_metrics_payload() == b"multiprocess"
    assert collected == [registry]


def test_multiprocess_metrics_aggregate_and_drop_dead_live_gauges(tmp_path):
    worker_code = """
import os
import sys
from core import observability
observability.HTTP_REQUESTS.labels(
    method="GET", route="/multiproc-test", status="200"
).inc()
observability.HTTP_LATENCY.labels(
    method="GET", route="/multiproc-test"
).observe(0.1)
observability.CHAT_ACTIVE_REQUESTS.set(float(sys.argv[1]))
observability.EVENT_LOOP_LAG_CURRENT.set(float(sys.argv[2]))
print(os.getpid())
"""
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    process_ids = []
    for active, lag in ((2, 0.01), (3, 0.02)):
        result = subprocess.run(
            [sys.executable, "-c", worker_code, str(active), str(lag)],
            cwd=project_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        process_ids.append(int(result.stdout.strip().splitlines()[-1]))

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
    payload = generate_latest(registry).decode("utf-8")
    assert (
        'blink_http_requests_total{method="GET",route="/multiproc-test",status="200"} 2.0'
        in payload
    )
    assert 'blink_chat_active_requests 5.0' in payload
    assert 'blink_event_loop_lag_current_seconds 0.02' in payload

    multiprocess.mark_process_dead(process_ids[0], path=str(tmp_path))
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
    payload = generate_latest(registry).decode("utf-8")
    assert 'blink_chat_active_requests 3.0' in payload
    assert 'blink_event_loop_lag_current_seconds 0.02' in payload
    assert (
        'blink_http_requests_total{method="GET",route="/multiproc-test",status="200"} 2.0'
        in payload
    )


def test_staging_launcher_fails_closed_for_unsafe_metrics_dir(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    launcher = project_root / "deploy" / "staging" / "run-backend.sh"
    subprocess.run(["bash", "-n", str(launcher)], check=True)
    env = os.environ.copy()
    env.update({
        "APP_ENV": "staging",
        "STAGING_WORKERS": "2",
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
    })
    result = subprocess.run(
        ["bash", str(launcher)],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "must stay under staging runtime" in result.stderr
