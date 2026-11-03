#!/usr/bin/env python3
"""
Generation smoke/parity tester for any OpenAI-compatible TAIDE endpoint.

Examples:
  python3 test_openai_generate.py --url http://127.0.0.1:8000
  python3 test_openai_generate.py --url http://127.0.0.1:8100
  python3 test_openai_generate.py --url http://127.0.0.1:8200
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_json(url: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    elapsed = time.perf_counter() - started
    return status, body, elapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Base URL, e.g. http://127.0.0.1:8100")
    ap.add_argument("--model", default="taide-12b")
    ap.add_argument("--cases", default=str(Path(__file__).with_name("taide_generate_cases.json")))
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    endpoint = args.url.rstrip("/") + "/v1/chat/completions"
    report = {
        "base_url": args.url,
        "endpoint": endpoint,
        "model": args.model,
        "results": [],
    }

    failed = False
    for case in cases:
        payload = {
            "model": args.model,
            "messages": case["messages"],
            "temperature": case.get("temperature", 0.0),
            "max_tokens": case.get("max_new_tokens", 64),
        }
        status, body, elapsed = request_json(endpoint, payload, args.timeout)

        row = {
            "name": case["name"],
            "status": status,
            "latency_s": round(elapsed, 4),
            "ok": False,
            "content": None,
            "usage": None,
            "error": None,
        }

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None

        if status == 200 and isinstance(parsed, dict):
            try:
                content = parsed["choices"][0]["message"]["content"]
                row["content"] = content
                row["usage"] = parsed.get("usage")
                row["ok"] = isinstance(content, str) and bool(content.strip())
                if case.get("expect_json") and row["ok"]:
                    json.loads(content)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["ok"] = False
        else:
            row["error"] = body[:2000]

        if not row["ok"]:
            failed = True

        report["results"].append(row)
        print(f"\n===== {case['name']} @ {args.url} =====")
        print(json.dumps(row, ensure_ascii=False, indent=2))

    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[OK] report={args.output}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
