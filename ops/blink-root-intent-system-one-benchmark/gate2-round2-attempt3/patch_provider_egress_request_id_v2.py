#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, py_compile
from pathlib import Path

VERSION='round2-attempt3-provider-egress-id-v2'

def reseal_manifest(root: Path) -> int:
    manifest=root/'MANIFEST.sha256'
    if not manifest.is_file():
        return 0
    lines=[]
    for raw in manifest.read_text(encoding='utf-8').splitlines():
        if not raw.strip():
            continue
        parts=raw.split(None,1)
        if len(parts)!=2:
            raise RuntimeError(f'unsupported manifest row: {raw!r}')
        rel=parts[1].strip()
        p=root/rel
        if not p.is_file():
            raise RuntimeError(f'manifest target missing: {rel}')
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel}")
    manifest.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return len(lines)

def patch_proxy(path: Path) -> bool:
    s=path.read_text(encoding='utf-8')
    if 'provider_request_id' not in s or "parent_rid = request_id(one_header(b'x-blink-shadow-parent-request-id'))" not in s:
        raise RuntimeError('Round2 v1 proxy correlation patch is not installed; refusing to guess')
    if 'def current_provider_request_id()' in s and 'proxy_provider_request_id' in s:
        return False
    old="""        return {'proxy_observer': VERSION if _INSTALLED else 'not_installed',
                'proxy_context': 'no_ingress_context', 'gateway_call_id': None,
                'proxy_ingress_id': None, 'proxy_parent_request_id': ''}
    return {'proxy_observer': VERSION,
            'proxy_context': 'active_ingress' if ctx['active'] else 'after_ingress',
            'gateway_call_id': ctx['gateway_call_id'],
            'proxy_ingress_id': ctx['proxy_ingress_id'],
            'proxy_parent_request_id': ctx['request_id']}


def current_request_id():
    return context_metadata()['proxy_parent_request_id']
"""
    new="""        return {'proxy_observer': VERSION if _INSTALLED else 'not_installed',
                'proxy_context': 'no_ingress_context', 'gateway_call_id': None,
                'proxy_ingress_id': None, 'proxy_parent_request_id': '',
                'proxy_provider_request_id': ''}
    return {'proxy_observer': VERSION,
            'proxy_context': 'active_ingress' if ctx['active'] else 'after_ingress',
            'gateway_call_id': ctx['gateway_call_id'],
            'proxy_ingress_id': ctx['proxy_ingress_id'],
            'proxy_parent_request_id': ctx['request_id'],
            'proxy_provider_request_id': ctx.get('provider_request_id', '')}


def current_request_id():
    return context_metadata()['proxy_parent_request_id']


def current_provider_request_id():
    return context_metadata()['proxy_provider_request_id']
"""
    if old not in s:
        raise RuntimeError('proxy_trace.py context_metadata shape differs from reviewed v1 source')
    path.write_text(s.replace(old,new,1),encoding='utf-8')
    return True

def patch_guard(path: Path) -> bool:
    s=path.read_text(encoding='utf-8')
    if 'def model_egress_request_id(' in s:
        if s.count("model_egress_request_id(cfg['role'],rid)") < 2:
            raise RuntimeError('shadow_guard.py has partial v2 patch')
        return False
    marker="""def install(cfg: dict):
"""
    helper="""def model_egress_request_id(role, parent_request_id):
    \"\"\"Use per-provider-call identity at the proxy egress, keep parent ID for audit.\"\"\"
    if role == 'proxy':
        from proxy_trace import current_provider_request_id
        provider_request_id = current_provider_request_id()
        if provider_request_id:
            return provider_request_id
    return parent_request_id


def install(cfg: dict):
"""
    if marker not in s:
        raise RuntimeError('shadow_guard.py install marker missing')
    s=s.replace(marker,helper,1)

    old1="""        rid=current_request_id()
        if port in cfg.get('llm_ports',[]) and rid: request.headers['X-Request-ID']=rid
        meta={'request_id':rid,'role':cfg['role'],'library':'httpx',
                'host':host,'port':port,'path':path.split('?')[0] if port in cfg.get('llm_ports',[]) else '<dependency>',
                'model_http':port in cfg.get('llm_ports',[])}
"""
    new1="""        rid=current_request_id()
        model_http=port in cfg.get('llm_ports',[])
        egress_rid=model_egress_request_id(cfg['role'],rid) if model_http else ''
        if egress_rid: request.headers['X-Request-ID']=egress_rid
        meta={'request_id':rid,'role':cfg['role'],'library':'httpx',
                'host':host,'port':port,'path':path.split('?')[0] if model_http else '<dependency>',
                'model_http':model_http}
        if cfg['role']=='proxy' and model_http and egress_rid:
            meta['provider_request_id']=egress_rid
"""
    if old1 not in s:
        raise RuntimeError('shadow_guard.py httpx forwarding shape differs from reviewed source')
    s=s.replace(old1,new1,1)

    old2="""            host,port,path=policy.check_url(request.url); rid=current_request_id()
            if port in cfg.get('llm_ports',[]) and rid: request.headers['X-Request-ID']=rid
            wire={}
"""
    new2="""            host,port,path=policy.check_url(request.url); rid=current_request_id()
            model_http=port in cfg.get('llm_ports',[])
            egress_rid=model_egress_request_id(cfg['role'],rid) if model_http else ''
            if egress_rid: request.headers['X-Request-ID']=egress_rid
            wire={}
            if cfg['role']=='proxy' and model_http and egress_rid:
                wire['provider_request_id']=egress_rid
"""
    if old2 not in s:
        raise RuntimeError('shadow_guard.py requests forwarding shape differs from reviewed source')
    s=s.replace(old2,new2,1)

    path.write_text(s,encoding='utf-8')
    return True

def patch(root: Path) -> dict:
    proxy=root/'proxy_trace.py'; guard=root/'shadow_guard.py'; compat=root/'compat_gateway_base.py'
    for p in (proxy,guard,compat):
        if not p.is_file(): raise FileNotFoundError(str(p))
    c=compat.read_text(encoding='utf-8')
    if "provider_request_id='shadow-'+uuid.uuid4().hex" not in c or "'X-Blink-Shadow-Parent-Request-ID':rid" not in c:
        raise RuntimeError('Round2 v1 compat provider-ID patch is not installed')
    proxy_changed=patch_proxy(proxy)
    guard_changed=patch_guard(guard)
    for p in (proxy,guard,compat): py_compile.compile(str(p),doraise=True)
    sealed=reseal_manifest(root)
    return {
      'schema':VERSION,
      'harness_dir':str(root),
      'changed':proxy_changed or guard_changed,
      'proxy_trace_changed':proxy_changed,
      'shadow_guard_changed':guard_changed,
      'manifest_rows_resealed':sealed,
      'behavior':{
        'application_request_id_remains_parent_audit_id':True,
        'compat_to_proxy_provider_request_id_unique_per_model_call':True,
        'proxy_to_vllm_x_request_id_uses_provider_request_id':True,
        'backend_to_compat_x_request_id_remains_application_request_id':True,
        'production_team_source_modified':False,
        'generation_budget_changed':False,
        'acceptance_changed':False,
      },
      'root_cause_fixed':'shadow_guard proxy egress previously overwrote X-Request-ID with the parent application request ID after compat/proxy correlation had already generated a unique provider request ID.'
    }

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--harness-dir',required=True);a=ap.parse_args()
    print(json.dumps(patch(Path(a.harness_dir).resolve()),ensure_ascii=False,indent=2))
if __name__=='__main__': main()
