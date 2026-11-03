"""Runs with the captured service's Python; reads configs, never imports the app."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import Refuse,atomic_json,read_json

def env_base(state_path,shadow_path,out):
    from dotenv import dotenv_values
    state=read_json(Path(state_path)); env=dict(state['env']); shadow=Path(shadow_path)
    # Isolated helper only: dotenv interpolation uses captured service env.
    os.environ.clear(); os.environ.update(env)
    root_env=shadow/'.env'
    if root_env.is_file():
        for k,v in dotenv_values(root_env).items():
            if v is not None: env.setdefault(k,v)
    atomic_json(Path(out),env)

def proxy_config(source,out,provider_port):
    import yaml
    data=yaml.safe_load(Path(source).read_text())
    # Latest snapshot config is a single local deployment, no DB/callback/router side effects.
    if not isinstance(data,dict) or set(data)!={'model_list'}:
        raise Refuse('Live LiteLLM config differs from reviewed single-deployment shape')
    entries=data['model_list']
    if not isinstance(entries,list) or len(entries)!=1:
        raise Refuse('Expected one LiteLLM deployment; refusing implicit fallback/routing changes')
    e=entries[0]
    if set(e)!={'model_name','litellm_params'} or e['model_name']!='taide-12b': raise Refuse('Unexpected proxy deployment')
    p=e['litellm_params']
    if set(p)!={'model','api_base','api_key'} or p['model']!='openai/taide-12b': raise Refuse('Unexpected LiteLLM parameters')
    # Only shadow config is changed. No router fallback or retry setting is introduced.
    p['api_base']=f'http://127.0.0.1:{int(provider_port)}/v1'
    p['api_key']='sk-shadow-local-no-cloud'
    Path(out).write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False),encoding='utf-8'); os.chmod(out,0o600)

def main():
    a=argparse.ArgumentParser(); sub=a.add_subparsers(dest='cmd',required=True)
    e=sub.add_parser('env'); e.add_argument('state'); e.add_argument('shadow'); e.add_argument('out')
    c=sub.add_parser('proxy'); c.add_argument('source'); c.add_argument('out'); c.add_argument('provider',type=int)
    args=a.parse_args()
    if args.cmd=='env': env_base(args.state,args.shadow,args.out)
    else: proxy_config(args.source,args.out,args.provider)
if __name__=='__main__': main()
