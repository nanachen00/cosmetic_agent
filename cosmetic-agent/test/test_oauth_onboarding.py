import pytest

from tools.auth.oauth_onboarding import (
    OnboardingTokenError,
    issue_onboarding_token,
    onboarding_cookie_samesite,
    onboarding_cookie_secure,
    verify_onboarding_token,
)


def test_onboarding_token_round_trip():
    token = issue_onboarding_token(user_id="user-test", provider="google", now=100)
    payload = verify_onboarding_token(token, now=101)

    assert payload["user_id"] == "user-test"
    assert payload["provider"] == "google"
    assert payload["exp"] == 700


def test_onboarding_token_rejects_tampering():
    token = issue_onboarding_token(user_id="user-test", provider="line", now=100)
    payload, signature = token.split(".", 1)
    tampered = f"{payload[:-1]}A.{signature}"

    with pytest.raises(OnboardingTokenError):
        verify_onboarding_token(tampered, now=101)


def test_onboarding_token_expires():
    token = issue_onboarding_token(
        user_id="user-test",
        provider="google",
        expires_in=10,
        now=100,
    )

    with pytest.raises(OnboardingTokenError, match="逾時"):
        verify_onboarding_token(token, now=110)


def test_onboarding_cookie_defaults_match_local_and_https_hosts(monkeypatch):
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)

    assert onboarding_cookie_secure("localhost") is False
    assert onboarding_cookie_samesite("localhost") == "lax"
    assert onboarding_cookie_secure("api.blinkai.tw") is True
    assert onboarding_cookie_samesite("api.blinkai.tw") == "none"
