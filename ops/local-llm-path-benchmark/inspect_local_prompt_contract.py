#!/usr/bin/env python3
"""Read-only, offline tokenizer/adapter diagnostic. Never loads a model or sends chat.

Synthetic markers distinguish adapter conversion from tokenizer rendering. A
failure here is NOT a full wire trace or a measurement of the running servers.
Only new report files below --output-dir (or diagnostics/) are written.
"""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import importlib.metadata
import io
import json
import os
from pathlib import Path
import sys
import uuid

MARKERS = {
    'user': 'USER_SENTINEL_1B3D5F',
    'instructions': 'SYSTEM_SENTINEL_2C4E6A',
    'schema': 'SCHEMA_SENTINEL_7A9C1E',
}

def digest(value: str | bytes) -> str:
    raw = value.encode('utf-8') if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()

def fixtures() -> list[tuple[str, str, dict]]:
    user = '只處理這則合成測試文字：' + MARKERS['user']
    instructions = '這是格式測試。請保留並回覆指定識別字：' + MARKERS['instructions']
    schema = {'type': 'object', 'properties': {'probe': {
        'type': 'string', 'enum': [MARKERS['schema']]}},
        'required': ['probe'], 'additionalProperties': False}
    return [
        ('user_only', '/v1/chat/completions', {
            'model': 'local-test-alias', 'messages': [{'role': 'user', 'content': user}],
            'max_tokens': 32}),
        ('chat_system', '/v1/chat/completions', {
            'model': 'local-test-alias', 'messages': [
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': user}], 'max_tokens': 32}),
        ('responses_instructions', '/v1/responses', {
            'model': 'local-test-alias', 'instructions': instructions,
            'input': [{'role': 'user', 'content': user}], 'max_output_tokens': 32}),
        ('responses_instructions_and_schema', '/v1/responses', {
            'model': 'local-test-alias', 'instructions': instructions,
            'input': [{'role': 'user', 'content': user}],
            'text': {'format': {'type': 'json_schema', 'name': 'probe',
                                'strict': True, 'schema': schema}},
            'max_output_tokens': 32}),
    ]

def markers_in(text: str) -> dict[str, bool]:
    return {name: marker in text for name, marker in MARKERS.items()}

def evaluate(name: str, api: str, original: dict, converted: dict, tokenizer) -> dict:
    messages = converted.get('messages', [])
    expected = markers_in(json.dumps(original, ensure_ascii=False))
    message_text = '\n'.join(str(m.get('content', '')) for m in messages)
    present = markers_in(message_text)
    row = {
        'name': name, 'request_api': api, 'synthetic_only': True,
        'expected_markers': expected, 'markers_after_adapter': present,
        'adapter_roles': [m.get('role') for m in messages],
        'adapter_content_chars': [len(str(m.get('content', ''))) for m in messages],
        'adapter_lost_markers': [k for k in expected if expected[k] and not present[k]],
    }
    try:
        rendered = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        if not isinstance(rendered, str):
            raise TypeError('apply_chat_template did not return a string')
        found = markers_in(rendered)
        lost = [k for k in expected if expected[k] and present[k] and not found[k]]
        row.update({
            'rendered_chars': len(rendered), 'rendered_sha256': digest(rendered),
            # Rendered text contains only this script's synthetic test content.
            'rendered_synthetic_text': rendered,
            'markers_after_template': found, 'template_lost_markers': lost,
            'rendering_error': None,
        })
        try:
            row['rendered_tokens_add_special_tokens_false'] = len(
                tokenizer.encode(rendered, add_special_tokens=False))
        except Exception as exc:
            row['token_count_error'] = {'type': type(exc).__name__, 'message': str(exc)[:500]}
        if row['adapter_lost_markers']:
            row['verdict'] = 'ADAPTER_CONTENT_LOSS'
        elif lost:
            row['verdict'] = 'LOCAL_TEMPLATE_CONTENT_LOSS'
        else:
            row['verdict'] = 'MARKER_RETENTION_PASS'
    except Exception as exc:
        row.update({'verdict': 'LOCAL_TEMPLATE_RENDER_ERROR',
                    'rendering_error': {'type': type(exc).__name__, 'message': str(exc)[:1000]}})
    return row

def load_converter(bundle: Path):
    path = bundle / 'compat_gateway.py'
    if not path.is_file() or not (bundle / 'common.py').is_file():
        raise FileNotFoundError('Expected compat_gateway.py and common.py in --bundle-dir')
    # The reviewed module defines a server but starts it only in its main guard.
    sys.path.insert(0, str(bundle))
    spec = importlib.util.spec_from_file_location('prompt_diagnostic_gateway', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load local compatibility module')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, 'convert', None)):
        raise RuntimeError('Local compatibility module has no convert() function')
    return mod.convert

def selftest() -> int:
    class FakeTokenizer:
        def __init__(self, drop=False, fail=False): self.drop=drop; self.fail=fail
        def apply_chat_template(self, messages, **kwargs):
            if self.fail: raise ValueError('fixture role rejection')
            return '\n'.join(m['content'] for m in messages
                             if not self.drop or m['role'] != 'system')
        def encode(self, text, **kwargs): return list(text)
    original={'instructions':MARKERS['instructions'],'input':MARKERS['user']}
    out={'messages':[{'role':'system','content':MARKERS['instructions']},
                     {'role':'user','content':MARKERS['user']}]}
    a=evaluate('x','/v1/responses',original,out,FakeTokenizer())
    assert a['verdict']=='MARKER_RETENTION_PASS'
    a=evaluate('x','/v1/responses',original,out,FakeTokenizer(drop=True))
    assert a['verdict']=='LOCAL_TEMPLATE_CONTENT_LOSS'
    assert a['template_lost_markers']==['instructions']
    a=evaluate('x','/v1/responses',original,out,FakeTokenizer(fail=True))
    assert a['verdict']=='LOCAL_TEMPLATE_RENDER_ERROR'
    a=evaluate('x','/v1/responses',original,{'messages':out['messages'][1:]},FakeTokenizer())
    assert a['verdict']=='ADAPTER_CONTENT_LOSS'
    assert len(fixtures())==4
    print('PASS: 5 synthetic diagnostic self-tests; no tokenizer/model/server used')
    return 0

def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bundle-dir',type=Path,default=Path(__file__).resolve().parent)
    ap.add_argument('--model-dir',type=Path,default=Path('/var/lib/jenkins/tomod/taide-12b'))
    ap.add_argument('--output-dir',type=Path)
    ap.add_argument('--selftest',action='store_true')
    args=ap.parse_args()
    if args.selftest:return selftest()
    bundle=args.bundle_dir.resolve(strict=True)
    model=args.model_dir.resolve(strict=True)
    # Offline for Hugging Face libraries; no weight loader is called.
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    os.environ['TOKENIZERS_PARALLELISM']='false'
    sys.dont_write_bytecode=True
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    out=args.output_dir or bundle/'diagnostics'/('prompt-'+stamp)
    out=out.absolute()
    if out==model or model in out.parents:
        raise ValueError('Output directory must not be inside the model directory')
    os.umask(0o077)
    out.mkdir(parents=True,exist_ok=False)
    report={
        'timestamp':datetime.now(timezone.utc).isoformat(),
        'scope':'offline synthetic adapter + tokenizer; NOT a live HTTP/model trace',
        'server_restarts':False,'http_requests':0,'model_weights_loaded':False,
        'python_executable':sys.executable,'python_version':sys.version,
        'bundle_dir':str(bundle),'model_dir':str(model),'cases':[],
    }
    rc=1
    try:
        # Socket-connect audit prevents accidental network use by Python clients.
        def offline_audit(event,args):
            if event in {'socket.connect','socket.getaddrinfo'}:
                raise RuntimeError('Offline diagnostic forbids network operations')
        sys.addaudithook(offline_audit)
        report['files']={}
        for path in [bundle/'compat_gateway.py',bundle/'common.py',
                     model/'tokenizer_config.json',model/'chat_template.jinja',
                     model/'config.json']:
            if path.is_file():
                report['files'][str(path)]={'sha256':digest(path.read_bytes()),'size':path.stat().st_size}
        convert=load_converter(bundle)
        with (out/'loader.log').open('w',encoding='utf-8') as log:
            with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
                from transformers import AutoTokenizer
                tokenizer=AutoTokenizer.from_pretrained(
                    str(model),local_files_only=True,trust_remote_code=False)
        report['transformers_version']=importlib.metadata.version('transformers')
        template=tokenizer.get_chat_template()
        report['tokenizer']={
            'class':type(tokenizer).__name__, 'is_fast':bool(tokenizer.is_fast),
            'template_sha256':digest(template), 'template_chars':len(template),
        }
        for name,api,payload in fixtures():
            try:
                converted,_=convert(payload,api)
                row=evaluate(name,api,payload,converted,tokenizer)
            except Exception as exc:
                row={'name':name,'verdict':'ADAPTER_ERROR','error':{
                    'type':type(exc).__name__,'message':str(exc)[:1000]}}
            report['cases'].append(row)
        report['all_markers_retained']=all(x['verdict']=='MARKER_RETENTION_PASS' for x in report['cases'])
        rc=0 if report['all_markers_retained'] else 2
    except Exception as exc:
        report['failure']={'type':type(exc).__name__,'message':str(exc)[:1000]}
    report['exit_code']=rc
    path=out/'prompt-contract.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('[REPORT]',path)
    for row in report['cases']:print(row['name'],row['verdict'])
    if 'failure' in report:print('[DIAGNOSTIC ERROR]',report['failure'])
    print('[EXIT]',rc,'(2 means observed synthetic contract failure; not a server failure)')
    return rc

if __name__=='__main__':
    raise SystemExit(main())
