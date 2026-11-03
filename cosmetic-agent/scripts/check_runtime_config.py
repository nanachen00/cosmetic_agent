#!/usr/bin/env python3
"""Validate OAuth/frontend URLs without printing credentials."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse


URL_KEYS = (
    "GOOGLE_CALLBACK_URL",
    "LINE_CALLBACK_URL",
    "FRONTEND_GOOGLE_SUCCESS_URL",
    "FRONTEND_LINE_SUCCESS_URL",
)


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=("development", "production"), required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args()

    for env_file in args.env_file:
        load_env_file(env_file)

    errors: list[str] = []
    expected_callback_host = "localhost" if args.environment == "development" else "api.blinkai.tw"
    expected_frontend_host = "localhost" if args.environment == "development" else "www.blinkai.tw"

    for key in URL_KEYS:
        value = os.getenv(key, "").strip()
        if not value:
            errors.append(f"{key}: missing")
            continue
        parsed = urlparse(value)
        expected_host = expected_frontend_host if key.startswith("FRONTEND_") else expected_callback_host
        actual_host = parsed.hostname or ""
        if actual_host not in ({"localhost", "127.0.0.1"} if expected_host == "localhost" else {expected_host}):
            errors.append(f"{key}: host must be {expected_host}, got {actual_host or '<invalid>'}")
        if args.environment == "production" and parsed.scheme != "https":
            errors.append(f"{key}: production URL must use https")

    if errors:
        print(f"CONFIG INVALID ({args.environment})")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"CONFIG OK ({args.environment})")
    for key in URL_KEYS:
        parsed = urlparse(os.environ[key])
        print(f"- {key}: {parsed.scheme}://{parsed.netloc}{parsed.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
