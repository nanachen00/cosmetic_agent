"""Private Shadow proxy ASGI/HTTP correlation; no payload or credential logging.

A fresh gateway call ID identifies one bridge -> proxy operation, while the
application request ID may have several such operations. The ID is not placed in
prompts, model parameters, or responses. Context-less proxy traffic is reported
separately; it never supplies evidence for a member request.
"""
from __future__ import annotations
import contextvars
import functools
import re
import time
import uuid
from common import event

HEADER = 'X-Blink-Shadow-Call-ID'
VERSION = 'proxy_asgi_context_v1'
CALL_PATTERN = re.compile(r'call-[a-f0-9]{32}')
_REQUEST = contextvars.ContextVar('blink_shadow_proxy_ingress', default=None)
_INSTALLED = False


def valid_call_id(value):
    return isinstance(value, str) and CALL_PATTERN.fullmatch(value) is not None


def request_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'(?:shadow-|witness-)[a-f0-9]{32}', value) else ''


def context_metadata():
    """Only generated identifiers and context state; never HTTP header dumps."""
    ctx = _REQUEST.get()
    if ctx is None:
        return {'proxy_observer': VERSION if _INSTALLED else 'not_installed',
                'proxy_context': 'no_ingress_context', 'gateway_call_id': None,
                'proxy_ingress_id': None, 'proxy_parent_request_id': ''}
    return {'proxy_observer': VERSION,
            'proxy_context': 'active_ingress' if ctx['active'] else 'after_ingress',
            'gateway_call_id': ctx['gateway_call_id'],
            'proxy_ingress_id': ctx['proxy_ingress_id'],
            'proxy_parent_request_id': ctx['request_id']}


def current_request_id():
    return context_metadata()['proxy_parent_request_id']


async def observe(app, scope, receive, send, audit_log):
    # Pure ASGI forwarding: no body buffering, new HTTP call, or BaseHTTPMiddleware.
    if scope.get('type') != 'http' or _REQUEST.get() is not None:
        return await app(scope, receive, send)
    headers = scope.get('headers', [])
    def one_header(name):
        values = [v for k, v in headers if k.lower() == name]
        if len(values) != 1:
            return ''
        try: return values[0].decode('ascii')
        except UnicodeError: return ''
    supplied = one_header(HEADER.lower().encode('ascii'))
    call_id = supplied if valid_call_id(supplied) else None
    rid = request_id(one_header(b'x-request-id'))
    ctx = {'gateway_call_id': call_id, 'proxy_ingress_id': 'ingress-' + uuid.uuid4().hex,
           'request_id': rid, 'active': True}
    token = _REQUEST.set(ctx)
    status = None
    start = time.perf_counter()
    meta = {k: v for k, v in ctx.items() if k != 'active'}
    # No URL query or arbitrary incoming path is emitted.
    meta.update(proxy_observer=VERSION,
                inference_path=scope.get('path') in {'/v1/chat/completions', '/chat/completions'},
                correlation_header_valid=bool(call_id))
    event(audit_log, 'proxy_request_started', **meta)
    async def observed_send(message):
        nonlocal status
        if message.get('type') == 'http.response.start':
            status = message.get('status')
        await send(message)
    try:
        return await app(scope, receive, observed_send)
    finally:
        ctx['active'] = False  # inherited deferred tasks must not look active
        _REQUEST.reset(token)
        event(audit_log, 'proxy_request_completed', **meta, status=status,
              duration_ms=(time.perf_counter() - start) * 1000)


def install(audit_log):
    """Wrap only FastAPI inside the already-owned proxy interpreter.

    This is installed before importing the proxy application. The wrapper keeps
    the original ASGI call/return/exception behavior, and covers mounted apps once.
    Runtime witness calls must prove this hook actually observed the proxy.
    """
    global _INSTALLED
    if _INSTALLED:
        raise RuntimeError('Proxy request observer already installed')
    try:
        import fastapi
    except ImportError:
        # Synthetic HTTP-only legacy fixtures need not install FastAPI. The
        # live A/B call-ID gate cannot pass without observed ASGI ingress.
        event(audit_log,'proxy_observer_unavailable',role='proxy',version=VERSION,reason='fastapi_unavailable')
        return
    original = fastapi.FastAPI.__call__
    @functools.wraps(original)
    async def wrapped(self, scope, receive, send):
        async def app(s, r, w):
            return await original(self, s, r, w)
        return await observe(app, scope, receive, send, audit_log)
    fastapi.FastAPI.__call__ = wrapped
    _INSTALLED = True
    event(audit_log, 'proxy_observer_installed', role='proxy', version=VERSION,
          body_modified=False, shared_service_modified=False)
