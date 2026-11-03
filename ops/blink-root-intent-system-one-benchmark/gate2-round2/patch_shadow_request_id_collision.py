#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, py_compile, re, shutil
from pathlib import Path

MARKER = "X-Blink-Shadow-Parent-Request-ID"


def reseal_manifest(root: Path) -> None:
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file():
        return
    lines=[]
    for raw in manifest.read_text(encoding='utf-8').splitlines():
        if not raw.strip():
            continue
        parts=raw.split(None,1)
        if len(parts)!=2:
            raise RuntimeError(f"unsupported manifest row: {raw!r}")
        rel=parts[1].strip()
        p=root/rel
        if not p.is_file():
            raise RuntimeError(f"manifest target missing: {rel}")
        h=hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{h}  {rel}")
    manifest.write_text("\n".join(lines)+"\n",encoding='utf-8')


def patch(root: Path) -> dict:
    base=root/'compat_gateway_base.py'
    proxy=root/'proxy_trace.py'
    for p in (base,proxy):
        if not p.is_file():
            raise FileNotFoundError(str(p))

    before_base=base.read_text(encoding='utf-8')
    before_proxy=proxy.read_text(encoding='utf-8')

    if MARKER in before_base and "provider_request_id" in before_base:
        base_changed=False
        after_base=before_base
    else:
        old="headers={'Authorization':'Bearer '+self.server.local_key,'X-Request-ID':rid,\n                          'X-Blink-Shadow-Call-ID':call_id}"
        new="provider_request_id='shadow-'+uuid.uuid4().hex\n            status,body,headers=http_json(self.server.upstream+'/v1/chat/completions',out,\n                          timeout=self.server.request_timeout,headers={'Authorization':'Bearer '+self.server.local_key,\n                          'X-Request-ID':provider_request_id, 'X-Blink-Shadow-Parent-Request-ID':rid,\n                          'X-Blink-Shadow-Call-ID':call_id}"
        # The replacement includes the call prefix, so replace the whole two-line call.
        old_full="status,body,headers=http_json(self.server.upstream+'/v1/chat/completions',out,\n                          timeout=self.server.request_timeout,"+old
        if old_full not in before_base:
            raise RuntimeError('compat_gateway_base.py does not match reviewed Round1 forwarding shape')
        after_base=before_base.replace(old_full,new,1)
        # Add only generated provider ID to model-call metadata for diagnostics.
        needle="event(self.server.audit_log,'model_call',request_id=rid,gateway_call_id=call_id,provider_port=self.server.provider_port,"
        repl="event(self.server.audit_log,'model_call',request_id=rid,provider_request_id=locals().get('provider_request_id'),gateway_call_id=call_id,provider_port=self.server.provider_port,"
        if needle not in after_base:
            raise RuntimeError('model_call audit shape not found')
        after_base=after_base.replace(needle,repl,1)
        base_changed=True

    if MARKER.lower() in before_proxy.lower() and "provider_request_id" in before_proxy:
        proxy_changed=False
        after_proxy=before_proxy
    else:
        old="""    rid = request_id(one_header(b'x-request-id'))\n    ctx = {'gateway_call_id': call_id, 'proxy_ingress_id': 'ingress-' + uuid.uuid4().hex,\n           'request_id': rid, 'active': True}\n"""
        new="""    provider_rid = request_id(one_header(b'x-request-id'))\n    parent_rid = request_id(one_header(b'x-blink-shadow-parent-request-id'))\n    rid = parent_rid or provider_rid\n    ctx = {'gateway_call_id': call_id, 'proxy_ingress_id': 'ingress-' + uuid.uuid4().hex,\n           'request_id': rid, 'provider_request_id': provider_rid, 'active': True}\n"""
        if old not in before_proxy:
            raise RuntimeError('proxy_trace.py does not match reviewed Round1 correlation shape')
        after_proxy=before_proxy.replace(old,new,1)
        proxy_changed=True

    # Defensive invariants: parent correlation remains generated-only; provider ID is per model call.
    if "provider_request_id='shadow-'+uuid.uuid4().hex" not in after_base:
        raise RuntimeError('unique provider request ID invariant missing')
    if "'X-Blink-Shadow-Parent-Request-ID':rid" not in after_base:
        raise RuntimeError('parent correlation header invariant missing')
    if "parent_rid = request_id(one_header(b'x-blink-shadow-parent-request-id'))" not in after_proxy:
        raise RuntimeError('proxy parent correlation mapping missing')
    if "rid = parent_rid or provider_rid" not in after_proxy:
        raise RuntimeError('proxy parent fallback mapping missing')

    backup=root/'round2-prepatch-request-id'
    backup.mkdir(exist_ok=True)
    if base_changed and not (backup/'compat_gateway_base.py').exists():
        shutil.copy2(base,backup/'compat_gateway_base.py')
    if proxy_changed and not (backup/'proxy_trace.py').exists():
        shutil.copy2(proxy,backup/'proxy_trace.py')
    if (root/'MANIFEST.sha256').is_file() and not (backup/'MANIFEST.sha256').exists():
        shutil.copy2(root/'MANIFEST.sha256',backup/'MANIFEST.sha256')

    base.write_text(after_base,encoding='utf-8')
    proxy.write_text(after_proxy,encoding='utf-8')
    py_compile.compile(str(base),doraise=True)
    py_compile.compile(str(proxy),doraise=True)
    reseal_manifest(root)

    result={
      'schema':'gate2-round2-request-id-collision-fix-v1',
      'harness_dir':str(root),
      'changed':bool(base_changed or proxy_changed),
      'compat_gateway_base_changed':base_changed,
      'proxy_trace_changed':proxy_changed,
      'behavior':{
        'application_request_id_preserved_for_audit':True,
        'provider_request_id_unique_per_model_call':True,
        'provider_request_id_format':'shadow-<uuid32>',
        'parent_correlation_header':MARKER,
        'production_team_logic_modified':False,
      },
      'round1_root_cause':'Multiple concurrent provider calls inherited the same X-Request-ID, and vLLM rejected duplicates with Request id ... already running.',
    }
    return result


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--harness-dir',required=True)
    a=ap.parse_args()
    result=patch(Path(a.harness_dir).resolve())
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
