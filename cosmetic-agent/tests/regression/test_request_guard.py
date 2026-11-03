from app.security.request_guard import FixedWindowLimiter, RateLimitRule


def test_fixed_window_limiter_allows_then_rejects() -> None:
    now = [100.0]
    limiter = FixedWindowLimiter(clock=lambda: now[0])
    rule = RateLimitRule(requests=2, window_seconds=60)

    assert limiter.check("chat:user-1", rule) == 0
    assert limiter.check("chat:user-1", rule) == 0
    assert limiter.check("chat:user-1", rule) == 60


def test_fixed_window_limiter_resets_after_window() -> None:
    now = [100.0]
    limiter = FixedWindowLimiter(clock=lambda: now[0])
    rule = RateLimitRule(requests=1, window_seconds=10)

    assert limiter.check("chat:user-1", rule) == 0
    now[0] = 105.5
    assert limiter.check("chat:user-1", rule) == 5
    now[0] = 110.0
    assert limiter.check("chat:user-1", rule) == 0


def test_fixed_window_limiter_keeps_keys_independent() -> None:
    limiter = FixedWindowLimiter(clock=lambda: 100.0)
    rule = RateLimitRule(requests=1, window_seconds=60)

    assert limiter.check("chat:user-1", rule) == 0
    assert limiter.check("chat:user-2", rule) == 0
    assert limiter.check("chat:user-1", rule) == 60


def test_rate_limit_rule_rejects_invalid_values() -> None:
    try:
        RateLimitRule(requests=0, window_seconds=60)
    except ValueError:
        pass
    else:
        raise AssertionError("zero request limit must be rejected")
