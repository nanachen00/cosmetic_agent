"""BLINK AI 線上 smoke / 對話回歸測試。

預設只測 health、ready、metrics、identity；加上 --chat 才會真的送出
Agent 對話，因此預設執行不呼叫模型，也不建立 GetZep 測試對話。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> tuple[int, str, Any]:
    body = None
    headers = {"Accept": "application/json", "X-Request-ID": f"reg-{uuid.uuid4().hex}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
            parsed = json.loads(text) if "json" in content_type else None
            return response.status, text, parsed
    except HTTPError as error:
        text = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        return error.code, text, parsed


def add_check(
    results: list[CheckResult],
    name: str,
    action: Callable[[], tuple[bool, str]],
) -> None:
    try:
        ok, detail = action()
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        ok, detail = False, f"{type(error).__name__}: {error}"
    results.append(CheckResult(name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="執行真正的 Agent 路由測試（會呼叫既有模型/GetZep）",
    )
    args = parser.parse_args()
    results: list[CheckResult] = []

    def check_health() -> tuple[bool, str]:
        status, _, data = request(args.base_url, "GET", "/api/v1/health")
        ok = status == 200 and isinstance(data, dict) and data.get("status") == "ok"
        return ok, f"HTTP {status}, body={data}"

    def check_ready() -> tuple[bool, str]:
        status, _, data = request(args.base_url, "GET", "/api/v1/ready")
        ok = status == 200 and isinstance(data, dict) and data.get("status") == "ready"
        return ok, f"HTTP {status}, body={data}"

    def check_metrics() -> tuple[bool, str]:
        status, text, _ = request(args.base_url, "GET", "/metrics")
        required = ("blink_http_requests_total", "blink_http_request_duration_seconds")
        missing = [name for name in required if name not in text]
        return status == 200 and not missing, f"HTTP {status}, missing={missing}"

    def check_identity() -> tuple[bool, str]:
        status, _, data = request(
            args.base_url,
            "POST",
            "/api/auth/identity",
            {"email": "regression-test@blink.invalid"},
        )
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        ok = status == 410 and "假登入已停用" in str(detail)
        return ok, f"HTTP {status}, detail={detail}"

    add_check(results, "health endpoint", check_health)
    add_check(results, "readiness endpoint", check_ready)
    add_check(results, "Prometheus metrics", check_metrics)
    add_check(results, "retired login identity is unavailable", check_identity)

    if args.chat:
        run_id = uuid.uuid4().hex[:12]

        def chat(case: str, message: str, flow: str | None = None) -> tuple[int, dict[str, Any]]:
            payload: dict[str, Any] = {
                "user_id": f"regression-{case}-{run_id}",
                "message": message,
            }
            if flow:
                payload["flow"] = flow
            status, text, data = request(
                args.base_url,
                "POST",
                "/api/chat/message",
                payload,
            )
            if not isinstance(data, dict):
                raise ValueError(f"API 未回傳 JSON object：{text[:200]}")
            return status, data

        def intent_name(data: dict[str, Any]) -> str:
            intent = data.get("intent") or {}
            return str(intent.get("intent_type") or "")

        def check_knowledge_route() -> tuple[bool, str]:
            status, data = chat("knowledge", "什麼是玻尿酸？")
            intent = intent_name(data)
            ok = status == 200 and intent == "knowledge_query"
            return ok, f"HTTP {status}, intent={intent}, stage={data.get('stage')}"

        def check_product_route() -> tuple[bool, str]:
            status, data = chat("product", "我想找洗面乳")
            intent = intent_name(data)
            ok = status == 200 and intent == "find_product"
            return ok, f"HTTP {status}, intent={intent}, stage={data.get('stage')}"

        def check_skin_tracking_route() -> tuple[bool, str]:
            status, data = chat("skin", "我長痘痘", flow="skin_record")
            stage = str(data.get("stage") or "")
            route = str(data.get("route") or "")
            ok = status == 200 and stage.startswith("skin_tracking") and route == "skin_record"
            return ok, f"HTTP {status}, stage={stage}, route={route}"

        def check_experience_topic_switch() -> tuple[bool, str]:
            user_id = f"regression-experience-{run_id}"

            def send(message: str) -> dict[str, Any]:
                status, text, data = request(
                    args.base_url,
                    "POST",
                    "/api/chat/message",
                    {"user_id": user_id, "message": message},
                )
                if status != 200 or not isinstance(data, dict):
                    raise ValueError(f"HTTP {status}: {text[:200]}")
                return data

            first = send("我買過美白面膜")
            switched = send("我想問A酸的功用是什麼？")
            first_stage = str(first.get("stage") or "")
            switched_intent = intent_name(switched)
            ok = first_stage.startswith("experience_") and switched_intent == "knowledge_query"
            return ok, f"first_stage={first_stage}, switched_intent={switched_intent}"

        add_check(results, "knowledge query routes to knowledge", check_knowledge_route)
        add_check(results, "product search routes to product", check_product_route)
        add_check(results, "skin flow stays in skin tracking", check_skin_tracking_route)
        add_check(results, "experience follow-up can switch topic", check_experience_topic_switch)

    failed = [result for result in results if not result.ok]
    print(f"\nSummary: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
