"""External ASGI observation wrapper; underlying API/ChatGuard/Agent unchanged."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sys
import time
from common import atomic_json,event
from shadow_guard import TRACE_ID
from identity_observer import observe_identity_imports

spec=json.loads(Path(os.environ['BLINK_SHADOW_POLICY_FILE']).read_text())
policy=spec['policy']; evidence=Path(spec['evidence_dir'])
# Observe the ORIGINAL resolver as soon as its definition/import binds, before
# a router factory/default argument/Depends captures the callable.
with observe_identity_imports(Path(policy['shadow_root']), evidence,
                              snapshot_id=spec.get('snapshot_id')) as identity_observer:
    import api.api_server as api_module
    identity_observer.assert_export(api_module._chat_user_id)
guard=api_module._CHAT_GUARD
from urllib.parse import urlparse
r=urlparse(guard.redis_url)
api_limiter=api_module._API_RATE_LIMITER
api_redis=urlparse(api_limiter._redis_url)
proof={'snapshot_id':spec.get('snapshot_id'),'api_module_file':str(Path(api_module.__file__).resolve()),'chat_guard_module_file':str(Path(sys.modules[type(guard).__module__].__file__).resolve()),
       'redis_host':r.hostname,'redis_port':r.port,'require_redis':guard.require_redis,
       'api_redis_port':api_redis.port,'api_require_redis':api_limiter._require_redis,
       'key_prefix':guard.key_prefix,'guest_limits':guard.guest_limits,'ip_limits':guard.ip_limits,
       'conversation_create_ip_limits':guard.conversation_create_ip_limits,
       'chat_max_running_env':os.getenv('CHAT_MAX_RUNNING'),
       'api_rate_limits':{method+' '+path:[rule.requests,rule.window_seconds] if hasattr(rule,'window_seconds') else str(rule)
                          for (method,path),rule in api_module._API_RATE_LIMITS.items()},
       'member_cookie_name':api_module.SESSION_COOKIE_NAME,
       'isolation_scope':'process_source_redis_only; other data services may be shared'}
if not proof['require_redis'] or proof['redis_port']!=spec['redis_port'] or proof['api_redis_port']!=spec['redis_port'] or not proof['api_require_redis']:
    raise RuntimeError('Shadow ChatGuard did not select dedicated Redis')
atomic_json(evidence/'guard-and-import-proof.json',proof)

_ALLOWED={('GET','/api/v1/live'),('GET','/api/v1/health'),('GET','/api/v1/ready'),('GET','/openapi.json'),
          ('POST','/api/auth/guest-session'),('POST','/api/auth/password/login'),
          ('GET','/api/auth/session'),('POST','/api/auth/logout'),('POST','/api/chat/message')}

async def app(scope,receive,send):
    if scope['type']!='http': return await api_module.app(scope,receive,send)
    if (scope['method'],scope['path']) not in _ALLOWED:
        await send({'type':'http.response.start','status':403,'headers':[(b'content-type',b'application/json')]})
        return await send({'type':'http.response.body','body':b'{"detail":"Outside benchmark scope"}'})
    headers=dict(scope.get('headers',[])); rid=headers.get(b'x-request-id',b'').decode(errors='replace')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',rid): rid=''
    token=TRACE_ID.set(rid); start=time.perf_counter(); status=None; headers_ms=None
    async def wrapped(message):
        nonlocal status,headers_ms
        if message['type']=='http.response.start':
            status=message['status']; headers_ms=(time.perf_counter()-start)*1000
        await send(message)
    try: await api_module.app(scope,receive,wrapped)
    finally:
        event(evidence/'asgi-timing.jsonl','asgi_request',request_id=rid,snapshot_id=spec.get('snapshot_id'),path=scope['path'],status=status,
              response_headers_ms=headers_ms,total_ms=(time.perf_counter()-start)*1000)
        TRACE_ID.reset(token)
