"""Run an existing service executable/module under its original Python, guarded."""
from __future__ import annotations
import json
import os
from pathlib import Path
import runpy
import sys

# -I omits this directory from sys.path; explicitly add only this trusted bundle.
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from common import Refuse

def main():
    spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    cfg=spec['policy']
    if cfg['role']=='backend':
        sys.path.insert(0,cfg['shadow_root'])
        # Never let captured PYTHONPATH re-introduce production application code.
        roots={Path(cfg['production_root']).resolve()}|{Path(p).resolve() for p in cfg.get('production_roots',[])}
        sys.path[:]=[p for p in sys.path if not p or not any(Path(p).resolve().is_relative_to(r) for r in roots)]
    from shadow_guard import install
    install(cfg)
    if cfg['role'] in {'backend','proxy'}:
        # Read real metadata, under the existing exact uname -p guard. Cache it
        # BEFORE app imports install wrappers. No fabricated platform values,
        # no generic subprocess permission, no production process changes.
        import platform
        from common import event
        platform.processor()
        platform.platform()
        event(cfg['audit_log'],'platform_metadata_warmup',role=cfg['role'],
              source='stdlib_real_local_metadata',before_application_import=True)
    os.environ['BLINK_SHADOW_POLICY_FILE']=sys.argv[1]
    argv=spec['service_argv']
    if spec['kind']=='module':
        sys.argv=[spec['entry'],*argv]; runpy.run_module(spec['entry'],run_name='__main__')
    elif spec['kind']=='script':
        sys.argv=[spec['entry'],*argv]; runpy.run_path(spec['entry'],run_name='__main__')
    else: raise Refuse('Unsupported launch kind')

if __name__=='__main__': main()
