#!/usr/bin/env python3
"""Bounded, localhost-only intent-schema inference probe; not an E2E benchmark.

Default/original: six serial generation requests, no retries, no login, no changes
to any service/config/source. --experiment raw-query-enum reuses a checksum-verified
prior synthetic plan and sends ONLY two :8200 requests, adding a singleton enum to
raw_query in the native decoding schema. Prompt text, labels, sampling parameters,
and response text are not rewritten. --baseline-dir is required for that mode. Original mode reads SYSTEM_PROMPT/INTENT_JSON_SCHEMA by ast.literal_eval,
uses the installed bundle's pure convert() function, then compares identical
prompt-only requests on :8000/:8200 with :8200 response_format=json_schema.
Only synthetic profile/query content is used. Existing model/GPU resources are
shared; a native schema request may cause server-side grammar compilation.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid

MAX_BYTES = 4 * 1024 * 1024
DEFAULT_ROOT = Path('/var/lib/jenkins/tomod')
SOURCE_REL = Path('cosmetic-agent/tools/agent/intent_parser.py')
LOOPBACK_PORTS = (8000, 8200)
# Only these trusted installed helper files are executed; never import backend.
HELPER_FILES = ('compat_gateway.py', 'common.py')
LOCK_NAMES = ('.verified-workflow.lock', '.benchmark.lock')
SCENARIOS = (
    ('product_search', '請推薦適合混合肌、無香料、1000 元內的清爽保濕產品。', 'find_product'),
    ('auto_root', '我想了解玻尿酸和神經醯胺的差別。', 'knowledge_query'),
)
PROFILE = ('【合成測試資料，非真實會員】\n'
           '膚質：混合肌；保養偏好：保濕、控油；預算：1000 元。\n'
           '請只以本輪輸入判定意圖；背景資料不代表使用者要求購買商品。')


class Refuse(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def atomic_json(path: Path, data) -> None:
    temp = path.with_name(path.name + '.pending')
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def stable_read(path: Path, limit: int = MAX_BYTES) -> bytes:
    # Read only one regular file. Source edits during capture are reported, not
    # silently combined. No symlink or FIFO traversal for private input paths.
    for parent in (path, *path.parents):
        if parent.is_symlink(): raise Refuse('Symlink refused: ' + str(parent))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        a = os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_size > limit:
            raise Refuse('Input must be a bounded regular file: ' + str(path))
        with os.fdopen(os.dup(fd), 'rb') as f: data = f.read(limit + 1)
        b = os.fstat(fd); c = path.stat()
        if len(data) > limit or (a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns) != (b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns) or (b.st_ino, b.st_dev, b.st_size, b.st_mtime_ns) != (c.st_ino, c.st_dev, c.st_size, c.st_mtime_ns):
            raise Refuse('Input changed during capture: ' + str(path))
        return data
    finally:
        os.close(fd)


@contextlib.contextmanager
def locks(bundle: Path):
    fds = []
    try:
        for name in LOCK_NAMES:
            p = bundle / name
            if p.is_symlink(): raise Refuse('Symlink lock refused')
            fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600); fds.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise Refuse('Invalid lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise Refuse('Workflow/recovery is active; no model requests sent') from None
        yield
    finally:
        for fd in reversed(fds): os.close(fd)


def pending_recovery(bundle: Path) -> None:
    for p in (bundle / '.state').glob('*/journal.json'):
        d = json.loads(stable_read(p).decode())
        # Match the installed lifecycle journal convention.
        if not d.get('cleanup_pass', False):
            raise Refuse('Pending recovery exists; do not run inference probe: ' + str(p.parent))


def load_contract(data: bytes):
    tree = ast.parse(data.decode('utf-8-sig'))
    found = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name) and t.id in {'SYSTEM_PROMPT', 'INTENT_JSON_SCHEMA'}:
                if t.id in found: raise Refuse('Multiple top-level assignments: ' + t.id)
                try: found[t.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError): raise Refuse('Contract is not a static literal: ' + t.id) from None
    if not isinstance(found.get('SYSTEM_PROMPT'), str) or not isinstance(found.get('INTENT_JSON_SCHEMA'), dict):
        raise Refuse('Expected SYSTEM_PROMPT and INTENT_JSON_SCHEMA literals were not found')
    schema = found['INTENT_JSON_SCHEMA']
    reject_external_refs(schema)
    return found['SYSTEM_PROMPT'], schema


def reject_external_refs(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {'$ref', '$dynamicRef', '$recursiveRef'} and (not isinstance(v, str) or not v.startswith('#')):
                raise Refuse('External schema references are not allowed in the probe')
            if k == '$schema' and v not in {
                'http://json-schema.org/draft-04/schema#',
                'http://json-schema.org/draft-06/schema#',
                'http://json-schema.org/draft-07/schema#',
                'https://json-schema.org/draft/2019-09/schema',
                'https://json-schema.org/draft/2020-12/schema',
            }:
                raise Refuse('Unknown external schema dialect')
            reject_external_refs(v)
    elif isinstance(value, list):
        for v in value: reject_external_refs(v)


def load_converter(bundle: Path):
    manifest = stable_read(bundle / 'MANIFEST.sha256').decode()
    entries = {}
    for line in manifest.splitlines():
        if not line.strip(): continue
        h, rel = line.split('  ', 1)
        if rel in entries: raise Refuse('Duplicate manifest entry')
        entries[rel] = h
    sources = {}
    provenance = {}
    for name in HELPER_FILES:
        data = stable_read(bundle / name)
        if digest(data) != entries.get(name): raise Refuse('Helper manifest mismatch: ' + name)
        sources[name] = data
        provenance[name] = digest(data)
    # Execute only the already manifest-checked, captured helper bytes. No
    # application module, database client or live source import occurs.
    import types
    common = types.ModuleType('common'); common.__file__ = str(bundle / 'common.py')
    previous = sys.modules.get('common')
    sys.modules['common'] = common
    try:
        exec(compile(sources['common.py'], common.__file__, 'exec'), common.__dict__)
        gateway = types.ModuleType('probe_gateway'); gateway.__file__ = str(bundle / 'compat_gateway.py')
        exec(compile(sources['compat_gateway.py'], gateway.__file__, 'exec'), gateway.__dict__)
    finally:
        if previous is None: sys.modules.pop('common', None)
        else: sys.modules['common'] = previous
    if not callable(getattr(gateway, 'convert', None)): raise Refuse('No compatible convert() entry')
    return gateway.convert, provenance


def build_payloads(convert, system: str, schema: dict, *, max_tokens: int, temperature: float, context: str):
    cases = []
    for name, query, expected_intent in SCENARIOS:
        content = '【本輪待判斷輸入】\n「' + query + '」'
        if context == 'synthetic_profile':
            content = ('【背景資料：只能參考，不得取代本輪意圖】\n' + PROFILE + '\n\n'
                       '【判斷要求】\n請優先判斷使用者本輪輸入。\n'
                       '如果本輪是新的知識問題，必須忽略上一輪尚未完成的商品話題。\n\n' + content)
        request = {'model': 'local-intent-probe', 'instructions': system,
                   'input': [{'role': 'user', 'content': content}],
                   'text': {'format': {'type': 'json_schema', 'name': 'intent_parse_result',
                                       'strict': True, 'schema': copy.deepcopy(schema)}},
                   'max_output_tokens': max_tokens, 'temperature': temperature,
                   'reasoning': {'effort': 'none'}, 'store': False}
        baseline, meta = convert(request, '/v1/responses')
        if [m.get('role') for m in baseline.get('messages', [])] != ['system', 'user']:
            raise Refuse('Expected repaired single-system-prefix converter; no requests sent')
        if meta.get('schema') != schema or any(k in baseline for k in ('response_format', 'guided_json', 'structured_outputs')):
            raise Refuse('Installed converter no longer matches the prompt-only baseline; do not silently compare')
        native = copy.deepcopy(baseline)
        native['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'intent_parse_result', 'strict': True, 'schema': copy.deepcopy(schema)}}
        cases.extend([
            {'scenario': name, 'query': query, 'expected_intent': expected_intent,
             'port': 8000, 'generation_mode': 'prompt_only', 'payload': copy.deepcopy(baseline)},
            {'scenario': name, 'query': query, 'expected_intent': expected_intent,
             'port': 8200, 'generation_mode': 'prompt_only', 'payload': copy.deepcopy(baseline)},
            {'scenario': name, 'query': query, 'expected_intent': expected_intent,
             'port': 8200, 'generation_mode': 'vllm_response_format_json_schema', 'payload': native},
        ])
    return cases


BASELINE_SCENARIOS = {name: (query, intent) for name, query, intent in SCENARIOS}
RAW_QUERY_MODE = 'vllm_response_format_json_schema_raw_query_enum'


def _verified_baseline_files(directory: Path):
    """Read a bounded, complete checksum list; never execute archived files."""
    if not directory.is_dir():
        raise Refuse('Baseline directory is missing; use the previous probe result directory')
    manifest = stable_read(directory / 'SHA256SUMS.txt').decode('utf-8')
    entries = {}
    for line in manifest.splitlines():
        if not line.strip():
            continue
        fields = line.split('  ', 1)
        if len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{64}', fields[0]):
            raise Refuse('Invalid baseline checksum entry')
        checksum, name = fields
        rel = Path(name)
        if (not name or rel.is_absolute() or '..' in rel.parts or
                len(rel.parts) != 1 or rel.name != name or name in entries):
            raise Refuse('Invalid or duplicate baseline checksum path')
        entries[name] = checksum
    if len(entries) > 32 or not {'plan.json', 'report.json'} <= entries.keys():
        raise Refuse('Baseline must contain checksummed plan.json and report.json')
    saved = {}
    for name, expected in entries.items():
        data = stable_read(directory / name)
        if digest(data) != expected:
            raise Refuse('Baseline checksum mismatch: ' + name)
        if name in {'plan.json', 'report.json'}:
            saved[name] = data
    return saved, entries


def load_bound_baseline(directory: Path, *, max_tokens: int, temperature: float,
                        context: str):
    """Narrow ONLY raw_query's allowed value in two verified native requests.

    The known input string is a protocol echo value, not the classification
    answer. All intent/skin/texture/etc. enums remain unchanged. Baseline query
    constants and metadata are checked to avoid reading real member payloads.
    Checksum integrity is not a signature or proof of a live model execution.
    """
    from jsonschema.validators import validator_for
    saved, checksums = _verified_baseline_files(directory)
    plan = strict_json(saved['plan.json'].decode('utf-8'))
    report = strict_json(saved['report.json'].decode('utf-8'))
    if not isinstance(plan, dict) or not isinstance(report, dict):
        raise Refuse('Baseline metadata must be JSON objects')
    if (report.get('scope') != 'direct local model schema experiment; NOT Shadow/E2E and NOT replay of member requests'
            or report.get('real_members_used') is not False
            or report.get('executed') is not True
            or report.get('completed_generation_requests') != 6
            or report.get('service_signals_sent') != 0
            or report.get('service_config_writes') != 0):
        raise Refuse('Expected a completed six-request synthetic-only baseline')
    if (report.get('max_tokens') != max_tokens or report.get('temperature') != temperature
            or report.get('context') != context):
        raise Refuse('Keep baseline max_tokens/temperature/context unchanged for this experiment')
    old_cases = plan.get('cases')
    old_results = report.get('results')
    if not isinstance(old_cases, list) or len(old_cases) != 6 or not isinstance(old_results, list) or len(old_results) != 6:
        raise Refuse('Baseline must contain exactly six planned and completed cases')
    grouped = {}
    result_map = {}
    for row in old_results:
        key = (row.get('scenario'), row.get('port'), row.get('generation_mode'))
        if key in result_map:
            raise Refuse('Duplicate baseline result identity')
        result_map[key] = row
    for item in old_cases:
        if not isinstance(item, dict):
            raise Refuse('Invalid baseline case')
        scenario = item.get('scenario')
        if scenario not in BASELINE_SCENARIOS:
            raise Refuse('Only the two existing synthetic probe scenarios are supported')
        query, expected_intent = BASELINE_SCENARIOS[scenario]
        if item.get('query') != query or item.get('expected_intent') != expected_intent:
            raise Refuse('Baseline does not use the declared fixed synthetic input')
        key = (item.get('port'), item.get('generation_mode'))
        g = grouped.setdefault(scenario, {})
        if key in g:
            raise Refuse('Duplicate baseline plan identity')
        g[key] = item
    required = {(8000, 'prompt_only'), (8200, 'prompt_only'),
                (8200, 'vllm_response_format_json_schema')}
    cases, base_schema = [], None
    previous_native = {}
    for scenario, (query, expected_intent) in BASELINE_SCENARIOS.items():
        group = grouped.get(scenario, {})
        if set(group) != required:
            raise Refuse('Missing or unexpected baseline comparison arm')
        native_item = group[(8200, 'vllm_response_format_json_schema')]
        payload = copy.deepcopy(native_item.get('payload'))
        if not isinstance(payload, dict):
            raise Refuse('Invalid native baseline payload')
        if set(payload) != {'model', 'messages', 'stream', 'max_tokens', 'temperature', 'response_format'}:
            raise Refuse('Unexpected native payload fields; do not infer a new protocol')
        if payload.get('model') != 'taide-12b' or payload.get('stream') is not False or payload.get('max_tokens') != max_tokens or payload.get('temperature') != temperature:
            raise Refuse('Baseline model/sampling parameters do not match')
        messages = payload.get('messages')
        if (not isinstance(messages, list) or [m.get('role') for m in messages] != ['system', 'user']
                or any(set(m) != {'role', 'content'} or not isinstance(m['content'], str) for m in messages)
                or query not in messages[-1]['content']):
            raise Refuse('Expected captured single-system and synthetic user messages')
        rf = payload.get('response_format')
        if not isinstance(rf, dict) or rf.get('type') != 'json_schema' or set(rf) != {'type', 'json_schema'}:
            raise Refuse('Expected response_format=json_schema')
        spec = rf.get('json_schema')
        if (not isinstance(spec, dict) or set(spec) != {'name', 'strict', 'schema'}
                or spec.get('name') != 'intent_parse_result' or spec.get('strict') is not True):
            raise Refuse('Expected the original native intent schema envelope')
        schema = spec.get('schema')
        if not isinstance(schema, dict):
            raise Refuse('Missing original intent schema')
        reject_external_refs(schema)
        validator_for(schema).check_schema(schema)
        if (schema.get('type') != 'object' or
                schema.get('properties', {}).get('raw_query') != {'type': 'string'} or
                'raw_query' not in schema.get('required', [])):
            raise Refuse('Expected required raw_query with exactly type:string; schema changed')
        if base_schema is None:
            base_schema = copy.deepcopy(schema)
        elif schema != base_schema:
            raise Refuse('Two baseline scenarios must share the identical original schema')
        if digest(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()) != report.get('schema_sha256'):
            raise Refuse('Baseline schema digest does not match report provenance')
        # Verify the old comparison truly differed only by response_format.
        old_prompt_only = copy.deepcopy(payload)
        old_prompt_only.pop('response_format')
        if any(group[(p, 'prompt_only')].get('payload') != old_prompt_only for p in (8000, 8200)):
            raise Refuse('Baseline arms differ beyond response_format')
        native_result = result_map.get((scenario, 8200, 'vllm_response_format_json_schema'))
        if not native_result or native_result.get('url') != 'http://127.0.0.1:8200/v1/chat/completions' or native_result.get('status') != 200:
            raise Refuse('Original native request was not recorded as accepted by :8200')
        # The sole experimental modification: constrain the echo before generation.
        schema['properties']['raw_query']['enum'] = [query]
        restored = copy.deepcopy(payload)
        restored['response_format']['json_schema']['schema']['properties']['raw_query'].pop('enum')
        if restored != native_item['payload']:
            raise Refuse('Internal error: payload changed beyond raw_query enum')
        cases.append({'scenario': scenario, 'query': query, 'expected_intent': expected_intent,
                      'port': 8200, 'generation_mode': RAW_QUERY_MODE, 'payload': payload})
        previous_native[scenario] = {
            'before': native_result.get('before'), 'after': native_result.get('after'),
            'body_ms': native_result.get('body_ms'), 'probe_pass': native_result.get('probe_pass'),
            'finish_reason': native_result.get('finish_reason'), 'usage': native_result.get('usage')}
    provenance = {
        'baseline_directory': str(directory), 'baseline_timestamp': report.get('timestamp'),
        'baseline_plan_sha256': checksums['plan.json'], 'baseline_report_sha256': checksums['report.json'],
        'source_path': report.get('source_path'), 'source_sha256': report.get('source_sha256'),
        'system_prompt_sha256': report.get('system_prompt_sha256'), 'schema_sha256': report.get('schema_sha256'),
        'helper_sha256_at_baseline': report.get('helper_sha256'),
        'current_backend_source_read': False, 'current_gateway_imported': False,
        'frozen_prompt_reused': True, 'expected_intent_is_not_encoded_as_singleton': True,
        'sole_request_delta': '/response_format/json_schema/schema/properties/raw_query/enum',
        'previous_native_results': previous_native,
    }
    return cases, base_schema, provenance


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(port: int, path: str, payload=None, timeout: float=120):
    if port not in LOOPBACK_PORTS or path not in {'/v1/models', '/v1/chat/completions'}:
        raise Refuse('Only reviewed loopback model endpoints are permitted')
    url = f'http://127.0.0.1:{port}{path}'
    rid = 'intent-probe-' + uuid.uuid4().hex
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json', 'X-Request-ID': rid, 'Authorization': 'Bearer EMPTY'}
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    started = now(); t0 = time.perf_counter(); status = None; raw = b''; err = None
    try:
        with opener.open(req, timeout=timeout) as response:
            status = int(response.status); raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code); raw = exc.read(MAX_BYTES + 1)
    except Exception as exc:
        err = type(exc).__name__  # No credentials/response content in metadata.
    elapsed = (time.perf_counter() - t0) * 1000
    parsed = None
    if len(raw) > MAX_BYTES:
        err = 'ResponseTooLarge'; raw = b''
    elif raw:
        try: parsed = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError): err = 'InvalidHTTPJSON'
    return {'request_id': rid, 'url': url, 'status': status, 'started_at': started,
            'finished_at': now(), 'body_ms': round(elapsed, 3), 'error': err,
            'attempts': 1, 'automatic_retry': False}, parsed, raw


def runtime_identity(port: int) -> dict:
    # Read-only, bounded observation. Failure is unknown, never fabricated idle.
    from shutil import which
    ss = which('ss')
    if not ss: return {'state': 'unknown', 'reason': 'ss_missing'}
    try:
        p = subprocess.run([ss, '-H', '-ltnp', f'sport = :{port}'], capture_output=True, text=True,
                           timeout=2, check=False, env={**os.environ, 'LC_ALL': 'C'})
        if p.returncode: return {'state': 'unknown', 'reason': 'ss_failed'}
        if not p.stdout.strip(): return {'state': 'not_listening'}
        pids = sorted(set(re.findall(r'pid=(\d+)', p.stdout)))
        if len(pids) != 1: return {'state': 'unknown', 'reason': 'owner_not_unique_or_visible'}
        proc = Path('/proc') / pids[0]
        start = proc.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
        argv = [s.decode(errors='replace') for s in proc.joinpath('cmdline').read_bytes().split(b'\0') if s]
        def flag(k):
            for i, arg in enumerate(argv):
                if arg.startswith(k+'='): return arg.split('=',1)[1]
                if arg == k and i+1 < len(argv): return argv[i+1]
            return None
        return {'state':'observed', 'pid':int(pids[0]), 'start_ticks':start,
                'argv_sha256':digest(json.dumps(argv).encode()),
                'max_num_seqs':flag('--max-num-seqs'), 'gpu_memory_utilization':flag('--gpu-memory-utilization')}
    except Exception as exc:
        return {'state':'unknown', 'reason':type(exc).__name__}


def strict_json(text):
    def reject_constant(_): raise ValueError('non_finite_number')
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d: raise ValueError('duplicate_key')
            d[k] = v
        return d
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=pairs)


def error_list(validator, parsed):
    out = []
    for e in validator.iter_errors(parsed):
        row = {'keyword':str(e.validator), 'path':list(e.absolute_path), 'schema_path':list(e.absolute_schema_path)}
        # Probe data is synthetic, never copied from live member profiles.
        if e.validator in {'enum','const','type'}:
            row['observed'] = e.instance
            row['expected'] = e.validator_value
        out.append(row)
        if len(out) >= 20: break
    return out


def evaluate(text, schema, validator, *, query: str, expected_intent: str):
    result = {'strict_json_pass':False, 'strict_schema_pass':False, 'wire_shape':'non_text',
              'strict_validation_errors':[], 'wrapper_candidate_json_pass':None,
              'wrapper_candidate_schema_pass':None, 'wrapper_validation_errors':[],
              'wrapper_removed_for_diagnostic_only':False, 'response_modified':False,
              'expected_intent_pass':None, 'raw_query_pass':None, 'task_contract_pass':False}
    if not isinstance(text, str): return result
    stripped = text.strip(); candidate = None
    fence = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        result['wire_shape'] = 'single_markdown_fence'; candidate = fence.group(1).strip()
    else:
        result['wire_shape'] = 'starts_object' if stripped.startswith('{') else 'starts_array' if stripped.startswith('[') else 'other_text'
    parsed = None
    try:
        parsed = strict_json(text); result['strict_json_pass'] = True; result['wire_shape'] = 'bare_json'
        errors = error_list(validator, parsed)
        result['strict_validation_errors'] = errors; result['strict_schema_pass'] = not errors
    except json.JSONDecodeError as exc:
        result['json_error'] = {'kind':'JSONDecodeError','line':exc.lineno,'column':exc.colno,'position':exc.pos}
    except (ValueError, TypeError) as exc:
        result['json_error'] = {'kind':type(exc).__name__}
    if candidate is not None:
        result['wrapper_removed_for_diagnostic_only'] = True
        try:
            v = strict_json(candidate); result['wrapper_candidate_json_pass'] = True
            errors = error_list(validator, v); result['wrapper_validation_errors'] = errors
            result['wrapper_candidate_schema_pass'] = not errors
        except (ValueError,TypeError):
            result['wrapper_candidate_json_pass'] = False; result['wrapper_candidate_schema_pass'] = False
    if isinstance(parsed,dict):
        result['expected_intent_pass'] = parsed.get('intent_type') == expected_intent
        result['raw_query_pass'] = parsed.get('raw_query') == query
    result['task_contract_pass'] = bool(result['strict_schema_pass'] and result['expected_intent_pass'] and result['raw_query_pass'])
    return result


def archive_report(out: Path) -> Path:
    files = sorted(p for p in out.rglob('*') if p.is_file())
    (out/'SHA256SUMS.txt').write_text(''.join(f'{digest(p.read_bytes())}  {p.relative_to(out).as_posix()}\n' for p in files),encoding='utf-8')
    archive = out.with_suffix('.tar.gz')
    with tarfile.open(archive,'w:gz') as t:
        t.add(out, arcname=out.name, recursive=True)
    archive.chmod(0o600)
    archive.with_name(archive.name+'.sha256').write_text(f'{digest(archive.read_bytes())}  {archive.name}\n',encoding='utf-8')
    return archive


def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--execute',action='store_true',help='Allow bounded local generation: original=6; raw-query-enum=2.')
    ap.add_argument('--experiment',choices=['original','raw-query-enum'],default='original',
                    help='original preserves the six-request probe; raw-query-enum reuses a frozen synthetic plan.')
    ap.add_argument('--baseline-dir',type=Path,default=None,
                    help='Prior intent-schema result directory containing checksummed plan.json and report.json.')
    ap.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    ap.add_argument('--bundle-dir',type=Path,default=Path(__file__).absolute().parent)
    ap.add_argument('--max-tokens',type=int,default=400)
    ap.add_argument('--temperature',type=float,default=0.1)
    ap.add_argument('--timeout',type=float,default=120)
    ap.add_argument('--cooldown-seconds',type=float,default=2)
    ap.add_argument('--context',choices=['none','synthetic_profile'],default='synthetic_profile')
    args=ap.parse_args(argv)
    if not 1 <= args.max_tokens <= 1024 or not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2:
        ap.error('Invalid generation parameters; max_tokens 1..1024, temperature 0..2')
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300 or not math.isfinite(args.cooldown_seconds) or not 0 <= args.cooldown_seconds <= 60:
        ap.error('Invalid bounded timeout/cooldown')
    if args.experiment == 'raw-query-enum' and args.baseline_dir is None:
        ap.error('--baseline-dir is required for --experiment raw-query-enum')
    if args.experiment == 'original' and args.baseline_dir is not None:
        ap.error('--baseline-dir is only used with --experiment raw-query-enum')
    bundle=args.bundle_dir.absolute(); root=args.root.absolute()
    if not bundle.is_dir(): raise Refuse('Bundle directory missing')
    try:
        from jsonschema.validators import validator_for
    except ImportError:
        print('[FAIL] jsonschema is missing in this Python. Use the existing harness Python; no package is installed.')
        return 1
    result={'scope':'direct local model schema experiment; NOT Shadow/E2E and NOT replay of member requests',
            'timestamp':now(),'executed':False,'source_imported':False,'real_members_used':False,
            'service_signals_sent':0,'service_config_writes':0,'planned_generation_requests':2 if args.experiment=='raw-query-enum' else 6,
            'experiment':args.experiment,
            'max_tokens':args.max_tokens,'temperature':args.temperature,'context':args.context,
            'native_schema_is_opt_in_probe_not_a_default_harness_change':True,'results':[]}
    out=None; code=1
    try:
        with locks(bundle):
            pending_recovery(bundle)
            source=None; data=None
            if args.experiment == 'raw-query-enum':
                cases,schema,provenance=load_bound_baseline(args.baseline_dir.absolute(),
                    max_tokens=args.max_tokens,temperature=args.temperature,context=args.context)
                result['baseline_provenance']=provenance
                result['raw_query_binding_scope']='known synthetic request input only; no classification label pinned'
            else:
                source=root/SOURCE_REL; data=stable_read(source)
                system,schema=load_contract(data)
                convert, provenance=load_converter(bundle)
                cases=build_payloads(convert,system,schema,max_tokens=args.max_tokens,temperature=args.temperature,context=args.context)
                result.update({'source_path':str(source),'source_sha256':digest(data),'system_prompt_sha256':digest(system.encode()),
                               'schema_sha256':digest(json.dumps(schema,sort_keys=True,ensure_ascii=False).encode()),
                               'helper_sha256':provenance})
            validator_type=validator_for(schema);validator_type.check_schema(schema);validator=validator_type(schema)
            prefix='intent-raw-query-' if args.experiment=='raw-query-enum' else 'intent-schema-'
            out=bundle/'diagnostics'/(prefix+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
            out.mkdir(parents=True,mode=0o700,exist_ok=False)
            result['schema_validator']=validator_type.__name__
            # Captured synthetic input only. The new experiment imports no live
            # backend/converter and neither changes the prior result directory.
            atomic_json(out/'plan.json',{'cases':cases,
                'live_source_was_only_statically_read':source is not None,
                'baseline_provenance':result.get('baseline_provenance')})
            if not args.execute:
                result['result']='PLAN_ONLY_NO_HTTP';code=0
            else:
                result['executed']=True; probes={}
                for port in sorted({case["port"] for case in cases}):
                    meta, body, _=http(port,'/v1/models',timeout=min(args.timeout,10))
                    ids=[x.get('id') for x in (body or {}).get('data',[]) if isinstance(x,dict)] if isinstance(body,dict) else []
                    probes[str(port)]={**meta,'model_ids':ids}
                    if meta['status']!=200 or 'taide-12b' not in ids:
                        atomic_json(out/'model-preflight.json',probes)
                        raise Refuse(f'Model endpoint :{port} unavailable or taide-12b not advertised; no generation phase')
                atomic_json(out/'model-preflight.json',probes)
                for i, case in enumerate(cases,1):
                    before=runtime_identity(case['port'])
                    meta,body,raw=http(case['port'],'/v1/chat/completions',case['payload'],timeout=args.timeout)
                    after=runtime_identity(case['port'])
                    try:
                        source_changed = (digest(stable_read(source)) != digest(data)) if source is not None else None
                    except (OSError, Refuse):
                        source_changed = None  # live deployment does not invalidate captured prompt bytes
                    choices=body.get('choices') or [] if isinstance(body,dict) else []
                    ch=choices[0] if choices and isinstance(choices[0],dict) else {}
                    msg=ch.get('message') or {}; text=msg.get('content') if isinstance(msg,dict) else None
                    ev=evaluate(text,schema,validator,query=case['query'],expected_intent=case['expected_intent'])
                    label=f'{i:02d}-{case["port"]}-{case["generation_mode"]}-{case["scenario"]}'
                    p=out/(label+'.response.json');p.write_bytes(raw or b'null\n');p.chmod(0o600)
                    usage=body.get('usage') if isinstance(body,dict) else None
                    completion=usage.get('completion_tokens') if isinstance(usage,dict) else None
                    row={'case':label,'port':case['port'],'scenario':case['scenario'],'generation_mode':case['generation_mode'],
                         **meta,'finish_reason':ch.get('finish_reason'),'usage':usage,'requested_max_tokens':args.max_tokens,
                         'budget_reached':isinstance(completion,int) and completion>=args.max_tokens,
                         'reply_chars':len(text) if isinstance(text,str) else None,'output_sha256':digest(text.encode()) if isinstance(text,str) else None,
                         'raw_response_file':p.name,'before':before,'after':after,
                         'process_observation':'NO_OBSERVED_EPOCH_CHANGE' if before.get('state')=='observed' and before==after else 'DRIFT_OR_UNKNOWN',
                         'source_changed_since_capture':source_changed,**ev}
                    if args.experiment == 'raw-query-enum':
                        # Independently verify BOTH the original and narrowed schema.
                        generated_schema=case['payload']['response_format']['json_schema']['schema']
                        gvtype=validator_for(generated_schema);gvtype.check_schema(generated_schema)
                        row['request_schema_pass']=False;row['request_schema_errors']=[]
                        if ev.get('strict_json_pass'):
                            ge=error_list(gvtype(generated_schema),strict_json(text))
                            row['request_schema_errors']=ge;row['request_schema_pass']=not ge
                        previous=provenance['previous_native_results'][case['scenario']]
                        row['baseline_native_result']=previous
                        row['same_model_epoch_as_baseline']=(before==previous.get('after')
                            if before.get('state')=='observed' and (previous.get('after') or {}).get('state')=='observed' else None)
                        row['payload_delta_verified_only_raw_query_enum']=True
                    row['probe_pass']=bool(meta['status']==200 and not meta['error'] and ev.get('task_contract_pass') and ch.get('finish_reason')=='stop' and not row['budget_reached']
                        and (row.get('request_schema_pass') is True if args.experiment=='raw-query-enum' else True))
                    result['results'].append(row)
                    atomic_json(out/'report.json',result)
                    print(f'[CASE] {label} HTTP={row["status"]} strict={row["strict_schema_pass"]} wrapper_candidate={row["wrapper_candidate_schema_pass"]} task={row["task_contract_pass"]} finish={row["finish_reason"]}',flush=True)
                    if before.get('state')=='observed' and after.get('state')=='observed' and before!=after:
                        raise Refuse('Model process changed during a request; retain completed results without resending')
                    if meta['status'] in (401,403,429,503) or meta['error'] or (args.experiment=='raw-query-enum' and meta['status']!=200):
                        raise Refuse('Endpoint rejected or failed this request; no automatic retry or additional load')
                    if i < len(cases):time.sleep(args.cooldown_seconds)
                result['result']='ALL_PROBES_PASS' if all(x['probe_pass'] for x in result['results']) else 'OBSERVED_CONTRACT_FAILURES'
                code=0 if result['result']=='ALL_PROBES_PASS' else 2
    except KeyboardInterrupt:
        result['result']='INTERRUPTED';code=130
    except Exception as exc:
        result['result']='PROBE_BLOCKED';result['failure']={'type':type(exc).__name__,'message':str(exc)[:500]};code=1
    finally:
        result['finished_at']=now();result['exit_code']=code
        result['completed_generation_requests']=len(result['results'])
        if out is not None:
            atomic_json(out/'report.json',result)
            archive=archive_report(out)
            print('[REPORT]',out/'report.json');print('[ARCHIVE]',archive)
        elif result.get('failure'):print('[FAIL]',result['failure'])
    print('[EXIT]',code,'(2 = observed synthetic schema/task failure; not cleanup failure)')
    return code


if __name__=='__main__':
    raise SystemExit(main())
