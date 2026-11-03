#!/usr/bin/env python3
"""Fail closed before a staging process can bind or use production state."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

PRODUCTION_PORTS = {8000, 8001, 8002, 8003, 8010, 8200}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
BASE_REQUIRED = (
    "DATABASE_URL",
    "REDIS_URL",
    "ZEP_API_KEY",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "CHAT_GUEST_SESSION_SECRET",
    "METRICS_BEARER_TOKEN",
)
OAUTH_REQUIRED = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_CALLBACK_URL",
    "LINE_CHANNEL_ID",
    "LINE_CHANNEL_SECRET",
    "LINE_CALLBACK_URL",
    "OAUTH_ONBOARDING_SECRET",
    "AUTH_SESSION_COOKIE_NAME",
    "OAUTH_ONBOARDING_COOKIE_NAME",
    "OAUTH_LINK_COOKIE_NAME",
    "CHAT_GUEST_COOKIE_NAME",
    "CORS_ALLOW_ORIGINS",
    "SESSION_TRUSTED_ORIGINS",
)
SECRET_IDENTIFIERS = (
    "ZEP_API_KEY",
    "NEO4J_PASSWORD",
    "CHAT_GUEST_SESSION_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "LINE_CHANNEL_ID",
    "LINE_CHANNEL_SECRET",
    "OAUTH_ONBOARDING_SECRET",
)


def _clean(values: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key): str(value).strip()
        for key, value in values.items()
        if value is not None and str(value).strip()
    }


def load_production_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("production environment reference file is missing")
    return _clean(dotenv_values(path))


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _url(value: str, name: str):
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute URL")
    return parsed


def _same_database(staging: str, production: str) -> bool:
    left = _url(staging, "DATABASE_URL")
    right = _url(production, "production DATABASE_URL")
    left_identity = (left.hostname, left.port or 5432, left.path.strip("/"))
    right_identity = (right.hostname, right.port or 5432, right.path.strip("/"))
    return left_identity == right_identity


def _same_value(staging: Mapping[str, str], production: Mapping[str, str], name: str) -> bool:
    return bool(staging.get(name) and production.get(name) and staging[name] == production[name])


def validate_staging_environment(
    environment: Mapping[str, object], production: Mapping[str, object]
) -> list[str]:
    env = _clean(environment)
    prod = _clean(production)
    errors: list[str] = []

    if env.get("APP_ENV") != "staging":
        errors.append("APP_ENV must equal staging")
    if env.get("STAGING_CONFIRM_ISOLATED_DATA") != "1":
        errors.append("STAGING_CONFIRM_ISOLATED_DATA must equal 1")
    release = env.get("APP_RELEASE", "")
    if not release or release.lower() in {"dev", "unknown", "latest"}:
        errors.append("APP_RELEASE must be a unique immutable release")
    if release and release == prod.get("APP_RELEASE"):
        errors.append("APP_RELEASE must differ from production")

    host = env.get("STAGING_HOST", "127.0.0.1").lower()
    if host not in LOOPBACK_HOSTS:
        errors.append("STAGING_HOST must be loopback")
    try:
        port = int(env.get("STAGING_PORT", "18110"))
    except ValueError:
        errors.append("STAGING_PORT must be an integer")
    else:
        if port in PRODUCTION_PORTS:
            errors.append("STAGING_PORT collides with a managed production port")
        if not 1024 <= port <= 65535:
            errors.append("STAGING_PORT must be between 1024 and 65535")

    for name in BASE_REQUIRED:
        if not env.get(name):
            errors.append(f"{name} is required")

    for name in ("CHAT_ADMISSION_REQUIRE_REDIS", "CHAT_JOB_STORE_REQUIRE_REDIS"):
        if env.get(name) != "1":
            errors.append(f"{name} must equal 1")
    if env.get("APP_RELEASE_REQUIRED") != "1":
        errors.append("APP_RELEASE_REQUIRED must equal 1")
    if env.get("METRICS_ALLOW_LOOPBACK") != "0":
        errors.append("METRICS_ALLOW_LOOPBACK must equal 0 for staging")

    database_url = env.get("DATABASE_URL", "")
    if database_url:
        try:
            parsed_db = _url(database_url, "DATABASE_URL")
            if parsed_db.scheme not in {"postgresql", "postgres"}:
                errors.append("DATABASE_URL must use PostgreSQL")
            if not parsed_db.path.strip("/"):
                errors.append("DATABASE_URL must name a staging database")
            if prod.get("DATABASE_URL") and _same_database(database_url, prod["DATABASE_URL"]):
                errors.append("DATABASE_URL targets the production database")
            prod_db = urlsplit(prod.get("DATABASE_URL", ""))
            if prod_db.hostname and parsed_db.hostname == prod_db.hostname and parsed_db.username == prod_db.username:
                errors.append("DATABASE_URL must use a staging-specific database role")
        except ValueError as exc:
            errors.append(str(exc))

    redis_url = env.get("REDIS_URL", "")
    if redis_url:
        try:
            parsed_redis = _url(redis_url, "REDIS_URL")
            if parsed_redis.scheme not in {"redis", "rediss"}:
                errors.append("REDIS_URL must use redis or rediss")
            if (parsed_redis.hostname or "").lower() not in LOOPBACK_HOSTS:
                errors.append("REDIS_URL must use the isolated loopback instance")
            if (parsed_redis.port or 6379) != 6389:
                errors.append("REDIS_URL must use staging Redis port 6389")
        except ValueError as exc:
            errors.append(str(exc))
    if env.get("CHAT_JOB_REDIS_URL") and env.get("CHAT_JOB_REDIS_URL") != redis_url:
        errors.append("CHAT_JOB_REDIS_URL must match the isolated REDIS_URL")

    if _same_value(env, prod, "ZEP_API_KEY"):
        errors.append("ZEP_API_KEY matches production")
    if env.get("NEO4J_URI") and env.get("NEO4J_URI") == prod.get("NEO4J_URI"):
        errors.append("NEO4J_URI matches production")
    for name in ("NEO4J_USER", "NEO4J_PASSWORD", "CHAT_GUEST_SESSION_SECRET"):
        if _same_value(env, prod, name):
            errors.append(f"{name} matches production")

    if _truthy(env.get("STAGING_REQUIRE_OAUTH", "0")):
        for name in OAUTH_REQUIRED:
            if not env.get(name):
                errors.append(f"{name} is required when staging OAuth is enabled")
        for name in SECRET_IDENTIFIERS:
            if _same_value(env, prod, name):
                errors.append(f"{name} matches production")
        for name in ("GOOGLE_CALLBACK_URL", "LINE_CALLBACK_URL"):
            value = env.get(name, "")
            if value:
                try:
                    callback = _url(value, name)
                    if callback.scheme != "https" or callback.hostname != "staging-api.blinkai.tw":
                        errors.append(f"{name} must use https://staging-api.blinkai.tw")
                except ValueError as exc:
                    errors.append(str(exc))
        for name in ("AUTH_SESSION_COOKIE_NAME", "OAUTH_ONBOARDING_COOKIE_NAME", "OAUTH_LINK_COOKIE_NAME", "CHAT_GUEST_COOKIE_NAME"):
            if env.get(name) in {"blink_session", "blink_oauth_onboarding", "blink_oauth_link_provider", "blink_guest_session"}:
                errors.append(f"{name} must be staging-specific")
        for name in ("CORS_ALLOW_ORIGINS", "SESSION_TRUSTED_ORIGINS"):
            value = env.get(name, "")
            if "*" in {item.strip() for item in value.split(",")}:
                errors.append(f"{name} cannot contain wildcard")
            if "https://blinkai.tw" in value or "https://www.blinkai.tw" in value:
                errors.append(f"{name} cannot include production frontend origins")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--production-env-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
    )
    args = parser.parse_args()
    try:
        production = load_production_values(args.production_env_file)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    errors = validate_staging_environment(os.environ, production)
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        print("No connection strings or secret values were printed.")
        return 1
    print("[OK] staging environment is isolated and fail-closed")
    print("[OK] bind=loopback, redis=6389, production identifiers differ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
