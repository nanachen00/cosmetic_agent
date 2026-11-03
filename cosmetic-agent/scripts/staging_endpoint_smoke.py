#!/usr/bin/env python3
"""Smoke-test staging health, release identity, and metrics access control."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PRODUCTION_PORTS = {8000, 8001, 8002, 8003, 8010, 8200}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "staging-api.blinkai.tw"}


def validate_target(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("target must be loopback or staging-api.blinkai.tw")
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and (parsed.port or 80) in PRODUCTION_PORTS:
        raise ValueError("refusing to probe a managed production port")
    if parsed.hostname == "staging-api.blinkai.tw" and parsed.scheme != "https":
        raise ValueError("public staging target must use HTTPS")
    return base_url.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18110")
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()
    try:
        base_url = validate_target(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))

    token = os.getenv("METRICS_BEARER_TOKEN", "").strip()
    if not token:
        print("[FAIL] METRICS_BEARER_TOKEN is required but will not be printed")
        return 1

    report: dict[str, object] = {"base_url": base_url, "release": args.expected_release, "checks": {}}
    checks = report["checks"]
    assert isinstance(checks, dict)
    failures: list[str] = []
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        live = client.get(base_url + "/livez")
        checks["livez"] = live.status_code
        if live.status_code != 200 or live.json().get("status") != "ok":
            failures.append("livez")
        if live.headers.get("X-App-Release") != args.expected_release:
            failures.append("livez release header")

        ready = client.get(base_url + "/readyz")
        checks["readyz"] = ready.status_code
        ready_body = ready.json()
        checks["dependencies"] = ready_body.get("dependencies", {})
        if ready.status_code != 200 or ready_body.get("status") != "ready":
            failures.append("readyz")
        if ready_body.get("release") != args.expected_release:
            failures.append("readyz release")
        if any(value != "ok" for value in ready_body.get("dependencies", {}).values()):
            failures.append("readyz dependencies")

        public_metrics = client.get(base_url + "/metrics")
        wrong_metrics = client.get(
            base_url + "/metrics", headers={"Authorization": "Bearer deliberately-wrong"}
        )
        private_metrics = client.get(
            base_url + "/metrics", headers={"Authorization": "Bearer " + token}
        )
        checks["metrics"] = {
            "unauthenticated": public_metrics.status_code,
            "wrong_token": wrong_metrics.status_code,
            "authenticated": private_metrics.status_code,
        }
        if public_metrics.status_code != 404 or wrong_metrics.status_code != 404:
            failures.append("metrics public access")
        if private_metrics.status_code != 200 or "# HELP" not in private_metrics.text:
            failures.append("metrics authenticated access")

    report["verdict"] = "PASS" if not failures else "FAIL"
    report["failures"] = failures
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_path:
        Path(args.json_path).write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
