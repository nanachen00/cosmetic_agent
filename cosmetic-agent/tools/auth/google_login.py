"""Google OAuth 2.0 / OpenID Connect helpers.

RECONSTRUCTED 2026-10-21 from app/api/routes/oauth.py call sites.
Credentials come only from GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any
from urllib.parse import urlencode

import requests

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_TIMEOUT_SECONDS = float(os.getenv("OAUTH_HTTP_TIMEOUT_SECONDS", "10"))


class GoogleLoginConfigurationError(RuntimeError):
    pass


class GoogleLoginError(RuntimeError):
    pass


def _client_credentials() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise GoogleLoginConfigurationError("Google 登入尚未設定 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
    return client_id, client_secret


def create_google_state() -> str:
    return secrets.token_urlsafe(32)


def build_google_authorization_url(*, state: str, callback_url: str) -> str:
    client_id, _ = _client_credentials()
    query = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "true",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(query)}"


def exchange_google_code(*, code: str, callback_url: str) -> dict[str, Any]:
    client_id, client_secret = _client_credentials()
    if not code:
        raise GoogleLoginError("Google callback 缺少 code")
    response = requests.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback_url,
            "grant_type": "authorization_code",
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise GoogleLoginError(f"Google token 交換失敗（HTTP {response.status_code}）")
    return response.json()


def get_google_profile(*, access_token: str) -> dict[str, Any]:
    response = requests.get(
        GOOGLE_USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise GoogleLoginError(f"讀取 Google 個人資料失敗（HTTP {response.status_code}）")
    return response.json()


def create_blink_user_id_from_google(google_user_id: str) -> str:
    """Deterministic legacy id; PostgreSQL ignores it and issues a UUID."""
    digest = hashlib.sha256(f"google:{google_user_id}".encode("utf-8")).hexdigest()[:24]
    return f"google-{digest}"
