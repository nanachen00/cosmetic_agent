"""LINE Login v2.1 helpers.

RECONSTRUCTED 2026-10-21 from app/api/routes/oauth.py call sites.
Credentials come only from LINE_CHANNEL_ID / LINE_CHANNEL_SECRET.
The ID token is verified by LINE's official verify endpoint, which checks
signature, issuer, audience (channel id) and expiry.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any
from urllib.parse import urlencode

import requests

LINE_AUTHORIZATION_ENDPOINT = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_ENDPOINT = "https://api.line.me/oauth2/v2.1/token"
LINE_PROFILE_ENDPOINT = "https://api.line.me/v2/profile"
LINE_VERIFY_ENDPOINT = "https://api.line.me/oauth2/v2.1/verify"
_TIMEOUT_SECONDS = float(os.getenv("OAUTH_HTTP_TIMEOUT_SECONDS", "10"))


class LineLoginConfigurationError(RuntimeError):
    pass


class LineLoginError(RuntimeError):
    pass


def _channel_credentials() -> tuple[str, str]:
    channel_id = os.getenv("LINE_CHANNEL_ID", "").strip()
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    if not channel_id or not channel_secret:
        raise LineLoginConfigurationError("LINE 登入尚未設定 LINE_CHANNEL_ID / LINE_CHANNEL_SECRET")
    return channel_id, channel_secret


def create_line_state() -> str:
    return secrets.token_urlsafe(32)


def build_line_authorization_url(*, state: str, callback_url: str) -> str:
    channel_id, _ = _channel_credentials()
    query = {
        "response_type": "code",
        "client_id": channel_id,
        "redirect_uri": callback_url,
        "state": state,
        "scope": "profile openid email",
        "bot_prompt": os.getenv("LINE_BOT_PROMPT", "normal"),
    }
    return f"{LINE_AUTHORIZATION_ENDPOINT}?{urlencode(query)}"


def exchange_line_code(*, code: str, callback_url: str) -> dict[str, Any]:
    channel_id, channel_secret = _channel_credentials()
    if not code:
        raise LineLoginError("LINE callback 缺少 code")
    response = requests.post(
        LINE_TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url,
            "client_id": channel_id,
            "client_secret": channel_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise LineLoginError(f"LINE token 交換失敗（HTTP {response.status_code}）")
    return response.json()


def get_line_profile(*, access_token: str) -> dict[str, Any]:
    response = requests.get(
        LINE_PROFILE_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise LineLoginError(f"讀取 LINE Profile 失敗（HTTP {response.status_code}）")
    return response.json()


def verify_line_id_token(*, id_token: str, user_id: str | None = None) -> dict[str, Any]:
    channel_id, _ = _channel_credentials()
    data = {"id_token": id_token, "client_id": channel_id}
    if user_id:
        data["user_id"] = user_id
    response = requests.post(LINE_VERIFY_ENDPOINT, data=data, timeout=_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise LineLoginError(f"LINE ID token 驗證失敗（HTTP {response.status_code}）")
    payload = response.json()
    if str(payload.get("aud") or "") != channel_id:
        raise LineLoginError("LINE ID token audience 不符")
    return payload


def create_blink_user_id_from_line(*, line_user_id: str) -> str:
    """Deterministic legacy id; PostgreSQL ignores it and issues a UUID."""
    digest = hashlib.sha256(f"line:{line_user_id}".encode("utf-8")).hexdigest()[:24]
    return f"line-{digest}"
