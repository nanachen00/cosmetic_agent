from app.api.routes.oauth import OAuthRouteDependencies, create_oauth_router


def test_oauth_router_preserves_four_operations():
    noop = lambda *args, **kwargs: None
    router = create_oauth_router(OAuthRouteDependencies(*([noop] * 5)))
    operations = sorted(
        (method, route.path)
        for route in router.routes
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    )
    assert operations == [
        ("GET", "/api/auth/google/callback"),
        ("GET", "/api/auth/google/login"),
        ("GET", "/api/auth/line/callback"),
        ("GET", "/api/auth/line/login"),
    ]
