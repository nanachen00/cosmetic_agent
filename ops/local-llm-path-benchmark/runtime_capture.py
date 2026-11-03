"""Capture required *launch metadata* with bounded retry during deployment.
No stop/restart/health mutation, no guessing credentials or launch commands.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import time
from common import Refuse, atomic_json, sha_bytes, sha_file, utc
import process_control as pc


def bounded_capture(port:int, timeout:float=60) -> dict:
    deadline=time.monotonic()+timeout; last='unattempted'
    while True:
        try: return pc.capture(port)
        except (Refuse,OSError,ValueError) as exc: last=type(exc).__name__
        if time.monotonic()>=deadline:
            raise Refuse(f'CAPTURE_UNAVAILABLE :{port}; no usable launch metadata within {timeout}s ({last}). No service action taken')
        time.sleep(min(1,max(0,deadline-time.monotonic())))


def backend_args(record:dict) -> list[str]:
    argv=record['argv']
    if argv[1:4]==['-m','uvicorn','api.api_server:app']:
        tail=argv[4:]
    elif len(argv)>2 and Path(argv[1]).name=='uvicorn' and argv[2]=='api.api_server:app':
        tail=argv[3:]
    else: raise Refuse('Unsupported backend launch interface; need uvicorn api.api_server:app')
    if '--reload' in tail or pc.flag_value(tail,'--workers') not in (None,'1'):
        raise Refuse('Shadow supports a single non-reload worker; do not silently change concurrency semantics')
    # Explicit app-dir may reintroduce production imports; not needed by a shadow cwd.
    out=[];i=0
    while i<len(tail):
        if tail[i]=='--app-dir':i+=2;continue
        if tail[i].startswith('--app-dir='):i+=1;continue
        out.append(tail[i]);i+=1
    return out


def proxy_config_path(record:dict) -> Path:
    argv=record['argv']
    if len(argv)<2 or Path(argv[1]).name!='litellm':raise Refuse('Supported proxy launch is Python + LiteLLM CLI')
    value=pc.flag_value(argv,'--config')
    if not value: raise Refuse('Proxy must have explicit config path')
    p=(Path(record['cwd'])/value).resolve(strict=True)
    if not p.is_file():raise Refuse('Proxy config is not a regular file')
    return p


META_CODE=r'''
import importlib.metadata as md,json,sys,sysconfig,hashlib,pathlib
paths=[sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]
names={'fastapi','starlette','uvicorn','openai','httpx','litellm','torch','transformers','vllm','neo4j','pymongo','zep-cloud','redis','python-dotenv','jsonschema','pyyaml','numpy','pydantic'}
result={'python':sys.version,'executable':str(pathlib.Path(sys.executable).resolve()),'distributions':{}}
for d in md.distributions(path=paths):
 name=(d.metadata.get('Name') or '').lower().replace('_','-')
 if name not in names:continue
 text=d.read_text('METADATA') or ''
 result['distributions'][name]={'version':d.version,'metadata_sha256':hashlib.sha256(text.encode()).hexdigest()}
print(json.dumps(result,sort_keys=True))
'''

def runtime_metadata(executable:str) -> dict:
    # -S: no application, sitecustomize or site-package import hooks.
    r=subprocess.run([executable,'-I','-S','-B','-c',META_CODE],capture_output=True,text=True,timeout=15,check=False)
    if r.returncode:raise Refuse('Runtime metadata probe failed')
    result=json.loads(r.stdout)
    result['executable_sha256']=sha_file(Path(executable))
    return result


def runtime_changed(baseline:dict) -> dict:
    errors=[];changes=[]
    for exe,expected in baseline.items():
        try:
            actual=runtime_metadata(exe)
            if actual!=expected:changes.append(exe)
        except Exception as exc:errors.append({'executable':exe,'type':type(exc).__name__})
    return {'status':'RUNTIME_METADATA_DRIFT' if changes else 'RUNTIME_OBSERVATION_PARTIAL' if errors else 'NO_OBSERVED_RUNTIME_METADATA_DRIFT',
            'changed_executables':changes,'errors':errors,
            'scope':'executable hash and selected installed distribution METADATA, not an immutable copy of entire Python environments'}
