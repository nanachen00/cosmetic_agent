from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode


ONBOARDING_COOKIE_NAME = (
    os.getenv("OAUTH_ONBOARDING_COOKIE_NAME", "blink_oauth_onboarding").strip()
    or "blink_oauth_onboarding"
)
ONBOARDING_TOKEN_TTL_SECONDS = 10 * 60


class OnboardingTokenError(Exception):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_secret() -> bytes:
    configured = os.getenv("OAUTH_ONBOARDING_SECRET", "").strip()
    return configured.encode("utf-8") if configured else secrets.token_bytes(32)


_ONBOARDING_SECRET = _load_secret()


def issue_onboarding_token(
    *,
    user_id: str,
    provider: str,
    expires_in: int = ONBOARDING_TOKEN_TTL_SECONDS,
    now: int | None = None,
) -> str:
    if not user_id or provider not in {"google", "line"}:
        raise OnboardingTokenError("快速登入資料不完整")

    issued_at = int(time.time() if now is None else now)
    payload = {
        "exp": issued_at + expires_in,
        "iat": issued_at,
        "jti": secrets.token_urlsafe(12),
        "provider": provider,
        "user_id": user_id,
    }
    encoded_payload = _b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signature = hmac.new(
        _ONBOARDING_SECRET,
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def verify_onboarding_token(token: str, *, now: int | None = None) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = (token or "").split(".", 1)
        expected_signature = hmac.new(
            _ONBOARDING_SECRET,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied_signature = _b64decode(encoded_signature)
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise OnboardingTokenError("設定會員帳號的登入憑證無效")

        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
        current_time = int(time.time() if now is None else now)
        if int(payload.get("exp", 0)) <= current_time:
            raise OnboardingTokenError("設定會員帳號的登入憑證已逾時")
        if payload.get("provider") not in {"google", "line"} or not payload.get("user_id"):
            raise OnboardingTokenError("設定會員帳號的登入憑證不完整")
        return payload
    except OnboardingTokenError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OnboardingTokenError("設定會員帳號的登入憑證無效") from error


def build_onboarding_url(provider: str) -> str:
    if provider == "line":
        base_url = os.getenv(
            "FRONTEND_LINE_SUCCESS_URL",
            "http://localhost:3000/login/line-success",
        ).strip()
    elif provider == "google":
        base_url = os.getenv(
            "FRONTEND_GOOGLE_SUCCESS_URL",
            "http://localhost:3000/login/google-success",
        ).strip()
    else:
        raise OnboardingTokenError("不支援的登入來源")

    return base_url


def onboarding_cookie_secure(hostname: str = "") -> bool:
    configured = os.getenv("AUTH_COOKIE_SECURE", "").strip().lower()
    if configured:
        return configured in {
            "1",
            "true",
            "yes",
            "on",
        }
    return (hostname or "").lower() not in {"", "localhost", "127.0.0.1"}


def onboarding_cookie_samesite(hostname: str = "") -> str:
    return "none" if onboarding_cookie_secure(hostname) else "lax"
