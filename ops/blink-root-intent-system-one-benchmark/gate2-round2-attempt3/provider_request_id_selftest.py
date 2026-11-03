#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib, sys
from pathlib import Path

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--harness-dir',required=True);a=ap.parse_args()
    h=Path(a.harness_dir).resolve();sys.path.insert(0,str(h))
    pt=importlib.import_module('proxy_trace'); sg=importlib.import_module('shadow_guard')
    parent='shadow-'+'a'*32; provider='shadow-'+'b'*32; call='call-'+'c'*32; ingress='ingress-'+'d'*32
    token=pt._REQUEST.set({'gateway_call_id':call,'proxy_ingress_id':ingress,'request_id':parent,'provider_request_id':provider,'active':True})
    try:
        md=pt.context_metadata()
        assert md['proxy_parent_request_id']==parent,md
        assert md['proxy_provider_request_id']==provider,md
        assert pt.current_request_id()==parent
        assert pt.current_provider_request_id()==provider
        assert sg.model_egress_request_id('proxy',parent)==provider
        assert sg.model_egress_request_id('backend',parent)==parent
    finally:
        pt._REQUEST.reset(token)
    assert sg.model_egress_request_id('proxy',parent)==parent
    src=(h/'shadow_guard.py').read_text(encoding='utf-8')
    assert src.count("model_egress_request_id(cfg['role'],rid)")>=2
    assert "meta['provider_request_id']=egress_rid" in src
    assert "wire['provider_request_id']=egress_rid" in src
    print('Round2 Attempt3 provider-egress request-ID selftest: PASS')
if __name__=='__main__': main()
