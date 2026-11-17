import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.api_server import enforce_api_request_limits


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type,expected', [
    ('text/event-stream; charset=utf-8', 'no-store, no-transform'),
    ('application/json', 'no-store'),
    ('text/plain', 'no-store'),
])
async def test_sensitive_streams_preserve_no_transform_and_never_allow_caching(content_type, expected):
    request = Request({'type': 'http', 'method': 'GET', 'path': '/api/chat/jobs/fixture',
                       'headers': [], 'query_string': b'', 'client': ('127.0.0.1', 1),
                       'server': ('127.0.0.1', 8010), 'scheme': 'http'})
    async def call_next(request):
        return Response('', headers={'content-type': content_type, 'Cache-Control': 'public, max-age=3600'})
    response = await enforce_api_request_limits(request, call_next)
    assert response.headers['cache-control'] == expected
    assert response.headers['pragma'] == 'no-cache'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-frame-options'] == 'DENY'
