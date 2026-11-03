#!/usr/bin/env python3
"""Summarize normalized API route usage from structured backend logs."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_LOG_DIR = Path("/var/lib/jenkins/tomod/ops/runtime/logs")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iter_http_events(paths: Iterable[Path], cutoff: datetime | None = None) -> Iterator[dict]:
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if event.get("event") != "http_request":
                continue
            timestamp = parse_timestamp(event.get("timestamp"))
            if cutoff is not None and (timestamp is None or timestamp < cutoff):
                continue
            yield event


def summarize(events: Iterable[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "requests": 0,
            "server_errors": 0,
            "statuses": Counter(),
            "durations_ms": [],
            "last_seen": None,
        }
    )
    for event in events:
        method = str(event.get("http_method") or "UNKNOWN").upper()
        route = str(event.get("http_route") or "unmatched")
        bucket = buckets[(method, route)]
        bucket["requests"] += 1
        try:
            status = int(event.get("http_status") or 0)
        except (TypeError, ValueError):
            status = 0
        bucket["statuses"][str(status)] += 1
        if status >= 500:
            bucket["server_errors"] += 1
        try:
            bucket["durations_ms"].append(float(event.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            pass
        timestamp = parse_timestamp(event.get("timestamp"))
        if timestamp is not None and (bucket["last_seen"] is None or timestamp > bucket["last_seen"]):
            bucket["last_seen"] = timestamp

    rows = []
    for (method, route), bucket in buckets.items():
        durations = sorted(bucket.pop("durations_ms"))
        p95 = durations[max(0, math.ceil(len(durations) * 0.95) - 1)] if durations else None
        last_seen = bucket["last_seen"]
        rows.append({
            "method": method,
            "route": route,
            "requests": bucket["requests"],
            "server_errors": bucket["server_errors"],
            "statuses": dict(sorted(bucket["statuses"].items())),
            "p95_ms": round(p95, 2) if p95 is not None else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })
    return sorted(rows, key=lambda row: (-row["requests"], row["route"], row["method"]))


def default_paths(log_dir: Path) -> list[Path]:
    return sorted(path for path in log_dir.glob("backend-8010*.log*") if path.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="backend JSON log files")
    parser.add_argument("--days", type=int, default=30, help="UTC lookback window (default: 30)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="default log directory")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be greater than zero")
    paths = args.paths or default_paths(args.log_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    rows = summarize(iter_http_events(paths, cutoff=cutoff))
    if args.json:
        print(json.dumps({"days": args.days, "files": [str(path) for path in paths], "routes": rows}, indent=2))
        return 0
    print(f"API route usage: last {args.days} day(s); files={len(paths)}; routes={len(rows)}")
    print("requests  5xx  p95_ms  last_seen_utc                 method  route")
    for row in rows:
        p95 = "-" if row["p95_ms"] is None else f"{row['p95_ms']:.2f}"
        last_seen = row["last_seen"] or "-"
        print(f"{row['requests']:8d}  {row['server_errors']:3d}  {p95:>7}  {last_seen:29}  {row['method']:6}  {row['route']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
