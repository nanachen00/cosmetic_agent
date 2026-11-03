#!/usr/bin/env python3
"""Read-only legacy :8000 source inspection. Python 3.10+, standard library only.

No imports/execution of inspected application code, no inference/HTTP requests,
no process control, and no reads of .env, accounts or session state.
--read-token-cap-only explicitly permits a bounded initial-environment stream scan
for source-identified numeric token-cap keys only; unrelated values are discarded.
Only allowlisted AST structure, line references, numeric bounds and fingerprints
are exported. This is static evidence, not proof of effective in-memory behavior.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
import time
import tokenize

DEFAULT_ROOT = Path('/var/lib/jenkins/tomod/ops/local-llm-path-benchmark')
DEFAULT_SOURCE = Path('/var/lib/jenkins/tomod/taide_server_graphrag.py')
VERSION = 'legacy_output_limits_cap_resolution_v2'
MAX_SOURCE = 2 * 1024 * 1024
MAX_REPORT = 32 * 1024 * 1024
MAX_FINDINGS = 400
BUDGET = {'max_tokens', 'max_new_tokens', 'max_output_tokens',
          'max_completion_tokens', 'max_length', 'min_new_tokens', 'min_length'}
GENERATION = {'generate', 'pipeline', 'SamplingParams', 'GenerationConfig'}
RESPONSE_FIELDS = {'content', 'finish_reason', 'completion_tokens', 'prompt_tokens',
                   'total_tokens', 'generated_text', 'text', 'choices', 'message', 'usage'}
SAFE_WORDS = BUDGET | GENERATION | RESPONSE_FIELDS | {
    'min', 'max', 'int', 'float', 'str', 'bool', 'len', 'sum', 'round', 'abs',
    'get', 'getenv', 'environ', 'json', 'dumps', 'loads', 'dump', 'load', 'dict',
    'list', 'tuple', 'Field', 'default', 'ge', 'le', 'gt', 'lt',
    'encode', 'decode', 'batch_decode', 'strip', 'lstrip', 'rstrip', 'replace',
    'ensure_ascii', 'indent', 'separators', 'skip_special_tokens',
    'return_full_text', 'return_dict_in_generate', 'input_ids', 'sequences',
    'temperature', 'top_p', 'top_k', 'do_sample', 'eos_token_id', 'pad_token_id',
    'stop', 'length', 'completed', 'incomplete', 'content_filter', 'unicode_escape',
    'utf-8', 'utf8', 'ascii', 'response_format', 'json_schema', 'json_object',
    'status_code', 'JSONResponse', 'Response', 'stream',
}
SECRET_NAME = re.compile(r'(?i)(password|passwd|secret|api_?key|credential|cookie|'
                         r'authorization|email|phone|dsn|session|access_token|refresh_token|id_token)')
BLOCKED_PARTS = {'.private', '.state', '.git', '__pycache__', 'node_modules'}


class InspectionError(RuntimeError):
    """Only fixed, non-sensitive reason codes may be used."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_path(path: Path) -> Path:
    p = Path(os.path.abspath(path))
    if any(x in BLOCKED_PARTS or x.startswith('.env') for x in p.parts):
        raise InspectionError('protected_path_refused')
    if any(x.is_symlink() for x in [p, *p.parents]):
        raise InspectionError('symbolic_link_refused')
    return p


def read_stable(path: Path, limit: int) -> tuple[bytes, dict]:
    p = checked_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(p, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise InspectionError('not_a_regular_file')
        if before.st_size > limit:
            raise InspectionError('file_size_limit')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        final = os.stat(p, follow_symlinks=False)
        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        if len(raw) > limit or identity(before) != identity(after) or identity(after) != identity(final):
            raise InspectionError('file_changed_during_read')
        return raw, {'sha256': digest(raw), 'size_bytes': len(raw),
                     'mtime_ns': after.st_mtime_ns, 'read_stable': True}
    finally:
        os.close(fd)


def reason(exc: BaseException) -> str:
    if isinstance(exc, InspectionError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return 'permission_denied'
    if isinstance(exc, FileNotFoundError):
        return 'file_not_found'
    if isinstance(exc, (SyntaxError, UnicodeError)):
        return 'python_source_parse_failed'
    return 'inspection_error_' + type(exc).__name__


class Renderer(ast.NodeTransformer):
    """All unknown identifiers/literals are replaced, not merely regex scrubbed."""
    def __init__(self):
        self.symbols: dict[str, str] = {}

    def name(self, value: str) -> str:
        if value in SAFE_WORDS:
            return value
        if value not in self.symbols:
            self.symbols[value] = 'symbol_' + str(len(self.symbols) + 1).zfill(4)
        return self.symbols[value]

    def visit_Name(self, node):
        node.id = self.name(node.id)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = self.name(node.attr)
        return node

    def visit_keyword(self, node):
        if node.arg and SECRET_NAME.search(node.arg):
            node.arg = self.name(node.arg)
            node.value = ast.Constant(value='<sensitive argument omitted>')
            return node
        self.generic_visit(node)
        if node.arg:
            node.arg = self.name(node.arg)
        return node

    def visit_Dict(self, node):
        for i, key in enumerate(node.keys):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value not in SAFE_WORDS:
                node.keys[i] = ast.Constant(value='<nontechnical key omitted>')
                node.values[i] = ast.Constant(value='<nontechnical value omitted>')
            else:
                node.keys[i] = self.visit(key) if key is not None else None
                node.values[i] = self.visit(node.values[i])
        return node

    def visit_Constant(self, node):
        value = node.value
        if value is None or type(value) is bool:
            return node
        if type(value) in (int, float):
            if math.isfinite(value) and abs(value) <= 1000000:
                return node
            node.value = '<large numeric literal omitted>'
        elif isinstance(value, str):
            node.value = value if value in SAFE_WORDS else '<text literal omitted>'
        else:
            node.value = '<literal omitted>'
        return node

    def expression(self, node: ast.AST) -> str:
        if sum(1 for _ in ast.walk(node)) > 250:
            return '<expression omitted: AST size bound>'
        transformed = self.visit(copy.deepcopy(node))
        text = ast.unparse(ast.fix_missing_locations(transformed))
        return text if len(text) <= 4000 else '<expression omitted: rendered size bound>'


def leaf(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    return None


def budget_name(name: str | None) -> bool:
    if not name or SECRET_NAME.search(name):
        return False
    n = name.lower()
    return n in BUDGET or (('token' in n or 'output' in n or 'response' in n)
                          and any(s in n for s in ('max', 'limit', 'cap', 'length')))


def analyze_source(raw: bytes) -> dict:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    tree = ast.parse(raw.decode(encoding))
    renderer = Renderer()
    findings: list[dict] = []
    definitions: dict[str, list[tuple[ast.AST, ast.AST]]] = {}
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and not SECRET_NAME.search(target.id):
                    definitions.setdefault(target.id, []).append((node, node.value))

    def context(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return {'kind': type(node).__name__, 'symbol': renderer.name(node.name), 'line': node.lineno}
        return {'kind': 'module'}

    pending: set[str] = set()
    seen = set()
    omitted = 0
    def add(kind, owner, value, **fields):
        nonlocal omitted
        key = (kind, getattr(owner, 'lineno', 0), getattr(owner, 'col_offset', 0), fields.get('field'))
        if key in seen:
            return
        seen.add(key)
        if len(findings) >= MAX_FINDINGS:
            omitted += 1
            return
        pending.update(n.id for n in ast.walk(value) if isinstance(n, ast.Name)
                       and n.id in definitions and not SECRET_NAME.search(n.id))
        findings.append({'kind': kind, 'line': getattr(owner, 'lineno', None),
                         'end_line': getattr(owner, 'end_lineno', None),
                         'context': context(owner), 'expression': renderer.expression(value), **fields})

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = leaf(target)
                if budget_name(name):
                    add('budget_binding', node, node.value, field=renderer.name(name),
                        target=renderer.expression(target))
        if isinstance(node, ast.Call):
            fn = leaf(node.func)
            for kw in node.keywords:
                if kw.arg in BUDGET:
                    add('budget_argument', node, kw.value, field=kw.arg,
                        callable=renderer.expression(node.func))
                elif kw.arg == 'ensure_ascii':
                    add('unicode_serialization_option', node, kw.value, field='ensure_ascii',
                        callable=renderer.expression(node.func))
                elif kw.arg in {'content', 'finish_reason', 'completion_tokens', 'generated_text'}:
                    add('response_field_argument', node, kw.value, field=kw.arg,
                        callable=renderer.expression(node.func))
            if fn in GENERATION:
                add('generation_call', node, node, callable=renderer.expression(node.func))
            if fn in {'min', 'max'}:
                add('numeric_bound_candidate', node, node)
            if fn in {'dumps', 'dump'}:
                add('json_serialization_candidate', node, node,
                    ensure_ascii_explicit=any(k.arg == 'ensure_ascii' for k in node.keywords),
                    module_identity_and_runtime_use_verified=False)
            if fn in {'encode', 'decode'} and any(isinstance(a, ast.Constant) and a.value == 'unicode_escape' for a in node.args):
                add('unicode_escape_conversion', node, node)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            add('slice_candidate_units_unverified', node, node)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and key.value in RESPONSE_FIELDS | BUDGET):
                    add('dictionary_field', key, value, field=key.value)

    # Def-use candidates only: all same-name assignments are shown; no claim that
    # these paths executed, nor that one scope's binding is another scope's value.
    expanded = set()
    for _ in range(5):
        current = sorted(pending - expanded)
        if not current:
            break
        expanded.update(current)
        for name in current:
            for owner, value in definitions.get(name, []):
                add('referenced_binding_candidate', owner, value, field=renderer.name(name),
                    runtime_resolution_verified=False)
    findings.sort(key=lambda row: (row['line'] or 0, row['kind'], row.get('field', '')))
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module.split('.')[0])
    return {'status': 'PARSED', 'python_source_executed': False,
            'raw_source_exported': False, 'findings': findings,
            'finding_count': len(findings), 'findings_omitted_by_limit': omitted,
            'definition_expansion_depth_limit': 5,
            'runtime_dataflow_verified': False,
            '_local_import_candidates': sorted(set(imports))}


def source_bundle(main: Path) -> list[dict]:
    sources = []
    todo = [('main', main)]
    for label, path in todo:
        try:
            if path.suffix != '.py':
                raise InspectionError('only_python_source_allowed')
            raw, meta = read_stable(path, MAX_SOURCE)
            analysis = analyze_source(raw)
            local_imports = analysis.pop('_local_import_candidates')
            if label == 'main':
                # One hop, plain sibling modules only; no package/environment scan.
                for name in local_imports:
                    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) and not SECRET_NAME.search(name):
                        candidate = path.parent / (name + '.py')
                        if candidate != path and candidate.is_file() and len(todo) < 13:
                            todo.append(('dependency_' + str(len(todo)).zfill(2), candidate))
            source_meta = {'label': label, **meta, **analysis}
            # The public entrypoint name is known in the handoff. Imported paths
            # and arbitrary user-provided filenames are never included.
            if label == 'main' and path.name == DEFAULT_SOURCE.name:
                source_meta['known_entrypoint'] = DEFAULT_SOURCE.name
            sources.append(source_meta)
        except Exception as exc:
            sources.append({'label': label, 'status': 'NOT_INSPECTED', 'reason': reason(exc),
                            'python_source_executed': False, 'raw_source_exported': False})
    return sources


def inspect_listener(source: Path, proc_root: Path = Path('/proc')) -> dict:
    result = {'port': 8000, 'status': 'NOT_OBSERVED', 'owners': [],
              'process_environment_read': False, 'full_command_line_exported': False,
              'loaded_code_equals_disk_verified': False}
    inodes = set()
    try:
        for name in ('tcp', 'tcp6'):
            try:
                lines = (proc_root / 'net' / name).read_text().splitlines()[1:]
            except FileNotFoundError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) > 9 and parts[3] == '0A' and int(parts[1].rsplit(':', 1)[1], 16) == 8000:
                    inodes.add(parts[9])
        if not inodes:
            return result
        result['status'] = 'LISTENER_OBSERVED_OWNER_UNRESOLVED'
        deadline = time.monotonic() + 3.0
        denied = 0
        for pid_dir in proc_root.iterdir():
            if time.monotonic() > deadline:
                result['bounded_scan_timed_out'] = True
                break
            if not pid_dir.name.isdigit():
                continue
            try:
                matches = False
                for i, fd in enumerate((pid_dir / 'fd').iterdir()):
                    if i > 8192 or time.monotonic() > deadline:
                        break
                    try:
                        link = os.readlink(fd)
                    except OSError:
                        continue
                    if link.startswith('socket:[') and link[8:-1] in inodes:
                        matches = True
                        break
                if not matches:
                    continue
                with (pid_dir / 'cmdline').open('rb') as stream:
                    cmd = stream.read(65537)
                if len(cmd) > 65536:
                    raise InspectionError('command_metadata_size_limit')
                argv = [os.fsdecode(s) for s in cmd.split(b'\0') if s]
                cwd = Path(os.readlink(pid_dir / 'cwd'))
                script_matches = any(not a.startswith('-') and a.endswith('.py')
                                     and Path(os.path.abspath(cwd / a)) == Path(os.path.abspath(source))
                                     for a in argv[1:])
                result['owners'].append({'pid': int(pid_dir.name),
                                         'specified_entrypoint_argument_matches': script_matches})
            except (OSError, InspectionError):
                denied += 1
        result['metadata_unreadable_processes'] = denied
        if result['owners']:
            result['status'] = 'OWNER_METADATA_OBSERVED'
        return result
    except Exception as exc:
        result['status'] = 'OBSERVATION_UNAVAILABLE'
        result['reason'] = reason(exc)
        return result


def finite_number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e12 else None


def report_summary(report: dict) -> dict:
    """Use only numeric/boolean metrics and enumerated states, never text content."""
    rows = []
    for req in report.get('requests', []):
        if not isinstance(req, dict) or req.get('provider') != 8000:
            continue
        scenario = req.get('scenario')
        if scenario not in {'product_search', 'skin_consult', 'auto_root'}:
            continue
        for call in req.get('model_timeline', []):
            if not isinstance(call, dict):
                continue
            diag = call.get('contract_diagnostic') or {}
            lexical = diag.get('lexical_summary') or {}
            raw = diag.get('raw') or {}
            err = raw.get('parse_error') or {}
            row = {'scenario': scenario,
                   'full_path_pass': req.get('full_path_pass') is True,
                   'schema_validation': call.get('schema_validation') if call.get('schema_validation') in {'pass', 'fail'} else 'not_verified',
                   'finish_reason': call.get('finish_reason') if call.get('finish_reason') in {'stop', 'length', 'content_filter'} else 'UNKNOWN'}
            for key in ('output_chars', 'requested_max_tokens', 'effective_max_tokens', 'prompt_tokens', 'completion_tokens'):
                row[key] = finite_number(call.get(key))
            row['unicode_escape_count'] = finite_number(lexical.get('unicode_escape_count'))
            row['incomplete_unicode_escape_count'] = finite_number(lexical.get('incomplete_unicode_escape_count'))
            row['unclosed_containers'] = finite_number(lexical.get('unclosed_containers'))
            row['unterminated_string'] = lexical.get('unterminated_string') if type(lexical.get('unterminated_string')) is bool else None
            row['parse_error_code'] = err.get('code') if err.get('code') in {
                'invalid_unicode_escape', 'invalid_escape', 'other_json_syntax', 'expecting_value',
                'expecting_comma', 'expecting_colon', 'unterminated_string', 'extra_data'} else None
            row['parse_error_offset'] = finite_number(err.get('offset'))
            rows.append(row)
    return {'status': 'READ_FROM_EXISTING_EXPORT_NOT_RERUN', 'legacy_model_calls': rows,
            'full_comparison_ready': (report.get('validation') or {}).get('full_comparison_ready') is True,
            'raw_replies_exported': False}


def read_latest_report(root: Path) -> dict:
    try:
        raw, _ = read_stable(root / 'LAST_RUN', 4096)
        selected = Path(raw.decode('utf-8').strip())
        if not selected.is_absolute():
            selected = root / selected
        selected = checked_path(selected)
        if not selected.is_relative_to(root / 'runs'):
            raise InspectionError('LAST_RUN_outside_public_runs')
        data, meta = read_stable(selected / 'report-data.json', MAX_REPORT)
        report = json.loads(data)
        if not isinstance(report, dict):
            raise InspectionError('invalid_report_shape')
        return {**report_summary(report), 'report_sha256': meta['sha256']}
    except Exception as exc:
        return {'status': 'NOT_READ', 'reason': reason(exc), 'raw_replies_exported': False}


def write_export(root: Path, report: dict) -> tuple[Path, Path]:
    root = checked_path(root)
    if not root.is_dir():
        raise InspectionError('harness_directory_missing')
    base = checked_path(root / 'diagnostics')
    base.mkdir(mode=0o700, exist_ok=True)
    tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '-legacy-output-inspection'
    folder = base / tag
    folder.mkdir(mode=0o700)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    data_path = folder / 'inspection.json'
    fd = os.open(data_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    archive = base / (tag + '.tar.gz')
    fd, temp_name = tempfile.mkstemp(prefix='.' + tag, dir=base)
    try:
        with os.fdopen(fd, 'wb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz') as tf:
                item = tarfile.TarInfo('legacy-output-inspection/inspection.json')
                item.size = len(raw)
                item.mode = 0o600
                item.mtime = 0
                tf.addfile(item, io.BytesIO(raw))
        os.replace(temp_name, archive)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return data_path, archive


# Only these narrowly identified settings can cross the export boundary.
# /proc/PID/environ represents the initial exec environment, not live globals.
MAX_ENV_BYTES = 4 * 1024 * 1024
MAX_ENV_ENTRY = 128 * 1024
MAX_CAP_KEYS = 8


def cap_integer(value):
    """Normalize bounded decimal configuration, never export arbitrary values."""
    if type(value) is int:
        number = value
    elif isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            try:
                value = value.decode('ascii')
            except UnicodeError:
                return None
        if len(value) > 16 or not re.fullmatch(r'\s*[+-]?[0-9]{1,7}\s*', value):
            return None
        number = int(value)
    else:
        return None
    return number if -65536 <= number <= 65536 else None


def permitted_cap_key(name):
    return bool(isinstance(name, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', name)
                and 'TOKEN' in name and any(s in name for s in ('MAX', 'CAP', 'LIMIT'))
                and not SECRET_NAME.search(name))


def cap_configuration(raw):
    """Describe module cap expressions without importing or evaluating code."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    tree = ast.parse(raw.decode(encoding))
    os_aliases = {a.asname or a.name for n in tree.body if isinstance(n, ast.Import)
                  for a in n.names if a.name == 'os'}
    getenv_aliases = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                      and n.module == 'os' and n.level == 0 for a in n.names if a.name == 'getenv'}
    defs = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    defs.setdefault(target.id, []).append(n)
    # An aliased or rebound builtin/import is not assumed to retain its semantics.
    rebound = set(defs)
    rebound.update(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)))
    os_aliases -= rebound
    getenv_aliases -= rebound
    cap_uses = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'min':
            for arg in n.args:
                if isinstance(arg, ast.Name) and arg.id in defs and budget_name(arg.id):
                    cap_uses.setdefault(arg.id, []).append(n.lineno)

    def describe(node, trail=()):
        if len(trail) > 6:
            return {'kind': 'unresolved', 'reason': 'expression_depth'}
        if isinstance(node, ast.Constant) and cap_integer(node.value) is not None:
            return {'kind': 'integer', 'value': cap_integer(node.value)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            c = describe(node.operand, trail + ('unary',))
            if c.get('kind') == 'integer':
                return {'kind': 'integer', 'value': c['value'] * (-1 if isinstance(node.op, ast.USub) else 1)}
        if isinstance(node, ast.Name) and node.id in defs and node.id not in trail:
            owners = defs[node.id]
            if len(owners) == 1:
                return describe(owners[0].value, trail + (node.id,))
        if isinstance(node, ast.Call):
            fn = node.func
            is_getenv = ((isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                          and fn.value.id in os_aliases and fn.attr == 'getenv')
                         or (isinstance(fn, ast.Name) and fn.id in getenv_aliases))
            if is_getenv and 1 <= len(node.args) <= 2 and not node.keywords:
                key = node.args[0]
                if isinstance(key, ast.Constant) and permitted_cap_key(key.value):
                    default = describe(node.args[1], trail + ('default',)) if len(node.args) == 2 else {'kind': 'unresolved', 'reason': 'no_default'}
                    return {'kind': 'environment', 'key': key.value, 'default': default,
                            'lookup_line': node.lineno}
            if isinstance(fn, ast.Name) and fn.id in {'max', 'min', 'int'} and fn.id not in rebound and not node.keywords:
                if (fn.id == 'int' and len(node.args) == 1) or (fn.id in {'min', 'max'} and 1 <= len(node.args) <= 8):
                    return {'kind': fn.id, 'args': [describe(a, trail + (fn.id,)) for a in node.args]}
        return {'kind': 'unresolved', 'reason': 'unsupported_or_redacted_expression'}

    bindings = []
    for name, owners in defs.items():
        if not budget_name(name):
            continue
        for n in owners:
            expression = describe(n.value, (name,))
            bindings.append({'binding': 'cap_' + str(len(bindings) + 1).zfill(2),
                             'line': n.lineno, 'end_line': n.end_lineno,
                             'used_in_min_lines': sorted(set(cap_uses.get(name, []))),
                             'expression': expression, 'multiple_module_assignments': len(owners) != 1,
                             'loaded_value_verified': False})
    keys = sorted({n['key'] for b in bindings for n in descriptor_walk(b['expression'])
                   if n.get('kind') == 'environment'})
    if len(keys) > MAX_CAP_KEYS:
        return {'status': 'UNRESOLVED', 'reason': 'too_many_cap_keys', 'bindings': [], 'allowed_environment_keys': []}
    return {'status': 'DESCRIBED' if bindings else 'NOT_IDENTIFIED', 'bindings': bindings,
            'allowed_environment_keys': keys,
            'scope': 'module-level numeric token caps only; static binding candidates, not live globals'}


def descriptor_walk(node):
    yield node
    for child in node.get('args', []):
        yield from descriptor_walk(child)
    if 'default' in node:
        yield from descriptor_walk(node['default'])


def evaluate_cap_descriptor(node, observed):
    """Interpret our own numeric descriptor, never Python expressions."""
    kind = node.get('kind')
    if kind == 'integer':
        return node['value']
    if kind == 'environment':
        row = observed.get(node['key'], {})
        if row.get('status') == 'VALID_INTEGER':
            return row['value']
        if row.get('status') == 'UNSET':
            return evaluate_cap_descriptor(node['default'], observed)
        return None
    args = [evaluate_cap_descriptor(a, observed) for a in node.get('args', [])]
    if not args or any(x is None for x in args):
        return None
    if kind == 'int' and len(args) == 1:
        return int(args[0])
    if kind == 'max':
        return max(args)
    if kind == 'min':
        return min(args)
    return None


def read_selected_cap_environment(stream, keys, byte_limit=MAX_ENV_BYTES):
    """Scan bounded NUL records; discard ALL nonallowlisted entries.

    Linux exposes one environ stream, not a per-key API. Bytes for unrelated
    entries are necessarily traversed in transient buffers, never saved/exported.
    Do not represent this operation as 'no environment read'.
    """
    if len(keys) > MAX_CAP_KEYS or not all(permitted_cap_key(k) for k in keys):
        raise InspectionError('non_cap_environment_key_refused')
    allowed = {k.encode('ascii'): k for k in keys}
    found = {}
    pending = b''
    total = 0

    def accept(record):
        key, sep, value = record.partition(b'=')
        if not sep or key not in allowed:
            return
        name = allowed[key]
        if name in found:
            found[name] = {'status': 'DUPLICATE_KEY', 'value': None}
        else:
            number = cap_integer(value)
            found[name] = {'status': 'VALID_INTEGER' if number is not None else 'VALUE_WITHHELD_NOT_BOUNDED_INTEGER',
                           'value': number}

    while True:
        chunk = stream.read(min(4096, byte_limit - total + 1))
        total += len(chunk)
        if total > byte_limit:
            raise InspectionError('initial_environment_size_limit')
        if not chunk:
            if pending:
                accept(pending)
            break
        pending += chunk
        records = pending.split(b'\0')
        pending = records.pop()
        for record in records:
            if len(record) > MAX_ENV_ENTRY:
                raise InspectionError('initial_environment_entry_size_limit')
            accept(record)
        if len(pending) > MAX_ENV_ENTRY:
            raise InspectionError('initial_environment_entry_size_limit')
    return {k: found.get(k, {'status': 'UNSET', 'value': None}) for k in keys}


def process_start_ticks(raw, pid):
    # The comm field can itself contain spaces and parentheses.
    end = raw.rfind(b')')
    if end < 0 or raw.split(b' ', 1)[0] != str(pid).encode():
        raise InspectionError('process_stat_invalid')
    fields = raw[end + 1:].split()  # field 3 starts at index 0; field 22 at 19.
    if len(fields) < 20 or not fields[19].isdigit():
        raise InspectionError('process_stat_invalid')
    return int(fields[19])


def proc_bytes(dir_fd, name, limit):
    fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=dir_fd)
    try:
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise InspectionError('process_metadata_size_limit')
        return raw
    finally:
        os.close(fd)


def argv_matches_source(dir_fd, source):
    cmd = proc_bytes(dir_fd, 'cmdline', 65536)
    argv = [os.fsdecode(s) for s in cmd.split(b'\0') if s]
    cwd = Path(os.readlink('cwd', dir_fd=dir_fd))
    return any(not a.startswith('-') and a.endswith('.py')
               and Path(os.path.abspath(cwd / a)) == Path(os.path.abspath(source)) for a in argv[1:])


def observe_selected_caps(source, source_meta, cap_config, listener, *, read_token_cap_only=False, proc_root=Path('/proc')):
    keys = cap_config.get('allowed_environment_keys', [])
    out = {'status': 'STATIC_DEFAULTS_ONLY', 'requested_selected_environment_read': read_token_cap_only,
           'process_environment_read': False, 'initial_environment_stream_opened': False,
           'unrelated_environment_values_exported': False, 'owners': [],
           'loaded_code_equals_disk_verified': False,
           'current_python_global_values_verified': False,
           'initial_environment_is_not_current_runtime_configuration': True}
    matching = [x for x in listener.get('owners', []) if x.get('specified_entrypoint_argument_matches') is True]
    if len(matching) != 1:
        out['status'] = 'OWNER_AMBIGUOUS_OR_UNRESOLVED'
        return out
    owner = matching[0]
    pid = owner['pid']
    row = {'pid': pid, 'status': 'UNRESOLVED', 'initial_cap_values': {}, 'conditional_cap_values': [],
           'source_mtime_utc': dt.datetime.fromtimestamp(source_meta['mtime_ns'] / 1e9, dt.timezone.utc).isoformat(),
           'process_start_time_is_not_exec_time': True}
    out['owners'].append(row)
    dfd = None
    try:
        dfd = os.open(proc_root / str(pid), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        before = process_start_ticks(proc_bytes(dfd, 'stat', 16384), pid)
        if not argv_matches_source(dfd, source):
            raise InspectionError('entrypoint_changed_or_mismatched')
        row['start_ticks_since_boot'] = before
        hz = os.sysconf('SC_CLK_TCK')
        try:
            btime = next(int(s.split()[1]) for s in (proc_root / 'stat').read_text().splitlines() if s.startswith('btime '))
            started = btime + before / hz
            row['process_birth_utc_approx'] = dt.datetime.fromtimestamp(started, dt.timezone.utc).isoformat()
            # btime is integral seconds; avoid a false ordering inside that
            # rounding window. Process birth still is not last exec/code load.
            uncertainty = 1.0 + 1.0 / hz
            row['process_birth_time_uncertainty_seconds'] = uncertainty
            source_time = source_meta['mtime_ns'] / 1e9
            row['source_mtime_after_process_birth'] = (
                True if source_time > started + uncertainty else
                False if source_time <= started else None)
        except (ValueError, StopIteration, OSError):
            row['process_birth_utc_approx'] = None
            row['source_mtime_after_process_birth'] = None
        if read_token_cap_only and keys:
            envfd = os.open('environ', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=dfd)
            out['process_environment_read'] = True
            out['initial_environment_stream_opened'] = True
            with os.fdopen(envfd, 'rb') as stream:
                observed = read_selected_cap_environment(stream, keys)
        else:
            observed = {k: {'status': 'NOT_READ', 'value': None} for k in keys}
        after = process_start_ticks(proc_bytes(dfd, 'stat', 16384), pid)
        if before != after or not argv_matches_source(dfd, source):
            raise InspectionError('process_changed_during_read')
        raw_after, meta_after = read_stable(source, MAX_SOURCE)
        del raw_after
        if meta_after['sha256'] != source_meta['sha256']:
            raise InspectionError('source_changed_during_read')
        row['initial_cap_values'] = observed
        for binding in cap_config.get('bindings', []):
            no_override = {k: {'status': 'UNSET'} for k in keys}
            default_value = evaluate_cap_descriptor(binding['expression'], no_override)
            value = evaluate_cap_descriptor(binding['expression'], observed)
            if binding['multiple_module_assignments']:
                value = None
            row['conditional_cap_values'].append({
                'binding': binding['binding'], 'line': binding['line'],
                'static_value_if_all_cap_environment_keys_unset': default_value,
                'value_if_current_disk_expression_used_initial_environment': value,
                'below_requested_intent_floor_1024': value < 1024 if value is not None else None,
                'used_in_min_lines': binding['used_in_min_lines'],
                'live_effective_cap_verified': False})
        row['status'] = 'INITIAL_ENVIRONMENT_OBSERVED' if read_token_cap_only and keys else 'STATIC_DEFAULTS_ONLY'
        out['status'] = row['status']
        if read_token_cap_only and any(v['value_if_current_disk_expression_used_initial_environment'] is None
                                       for v in row['conditional_cap_values'] if v['used_in_min_lines']):
            out['status'] = row['status'] = 'CAP_VALUE_UNRESOLVED'
    except Exception as exc:
        row['status'] = out['status'] = 'OBSERVATION_UNAVAILABLE'
        row['reason'] = reason(exc)
        row['initial_cap_values'] = {}
        row['conditional_cap_values'] = []
    finally:
        if dfd is not None:
            os.close(dfd)
    return out


def collect(root: Path, source: Path, *, read_token_cap_only=False) -> tuple[dict, Path]:
    root = checked_path(root)
    source = checked_path(source)
    sources = source_bundle(source)
    listener = inspect_listener(source)
    summary = read_latest_report(root)
    try:
        cap_raw, cap_meta = read_stable(source, MAX_SOURCE)
        if cap_meta['sha256'] != sources[0].get('sha256'):
            raise InspectionError('source_changed_between_inspections')
        caps = cap_configuration(cap_raw)
        del cap_raw
        initial = observe_selected_caps(source, cap_meta, caps, listener,
                                        read_token_cap_only=read_token_cap_only)
    except Exception as exc:
        caps = {'status': 'UNRESOLVED', 'reason': reason(exc), 'bindings': [], 'allowed_environment_keys': []}
        initial = {'status': 'NOT_READ', 'process_environment_read': False, 'owners': []}
    result = {
        'tool': VERSION, 'status': 'STATIC_INSPECTION_COLLECTED' if sources[0]['status'] == 'PARSED' else 'STATIC_INSPECTION_INCOMPLETE',
        'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'sources': sources, 'listener': listener, 'existing_run': summary,
        'token_cap_configuration': caps, 'startup_cap_observation': initial,
        'limits_of_evidence': [
            'Static code candidates only; no model inference or HTTP request was executed.',
            'A numeric bound or slice may apply to input, tokens, output, telemetry or logs; its units and executed path are not inferred.',
            '600 reported tokens and an 800-character response do not alone prove an internal cap or clipping.',
            'Disk source hashes and listener argv matching do not prove the loaded Python code equals disk source.',
            'Initial environment is scanned ONLY when --read-token-cap-only is specified; only source-identified bounded numeric token-cap values survive export. Other entry bytes are transiently traversed and discarded.',
            '/proc/PID/environ is the initial exec environment; it does not verify current os.environ or Python global values. No target-process memory is read.',
            'Process birth time is not the last exec time. Neither timestamps nor matching argv prove loaded code matches current disk source.',
            'Same-name referenced bindings are syntax candidates, not verified scope-aware runtime dataflow.',
            'Only direct plain sibling imports are inspected (maximum 12); packages and transitive imports are not scanned.',
        ],
        'safety': {'inspected_code_executed': False, 'model_loaded': False,
                   'http_requests_sent': 0, 'source_or_harness_files_rewritten': False,
                   'shared_services_started_or_stopped': False,
                   'private_accounts_or_state_read': False, 'process_environment_read': initial.get('process_environment_read', False),
                   'complete_process_environment_exported': False,
                   'raw_source_or_reply_exported': False, 'interactive_html_generated': False},
    }
    result['listener']['process_environment_read'] = initial.get('process_environment_read', False)
    used_bindings = [b for b in caps.get('bindings', []) if b.get('used_in_min_lines')]
    resolved = [v for owner in initial.get('owners', []) for v in owner.get('conditional_cap_values', [])
                if v.get('used_in_min_lines')]
    result['cap_resolution_complete_under_stated_assumptions'] = (
        bool(used_bindings) and len(resolved) == len(used_bindings)
        and initial.get('status') == 'INITIAL_ENVIRONMENT_OBSERVED'
        and all(v['value_if_current_disk_expression_used_initial_environment'] is not None for v in resolved))
    if read_token_cap_only and not result['cap_resolution_complete_under_stated_assumptions']:
        result['status'] = 'INSPECTION_COLLECTED_WITH_UNRESOLVED_CAP'
    _, archive = write_export(root, result)
    return result, archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--read-token-cap-only', action='store_true',
                        help='Explicitly scan listener initial environment for source-identified numeric token caps; never export other entries')
    args = parser.parse_args()
    try:
        result, archive = collect(args.harness_root, args.source, read_token_cap_only=args.read_token_cap_only)
        print('[STATUS]', result['status'])
        print('[PATCH]', VERSION)
        print('[TOKEN CAP OBSERVATION]', result['startup_cap_observation']['status'])
        print('[INITIAL ENVIRONMENT STREAM SCANNED]', result['safety']['process_environment_read'])
        print('[MAIN SOURCE]', result['sources'][0]['status'])
        print('[STATIC FINDINGS]', sum(s.get('finding_count', 0) for s in result['sources']))
        print('[SERVICE CONTROL] none; [MODEL/HTTP REQUESTS] 0')
        print('[LEGACY INSPECTION ARCHIVE]', archive)
        print('[ARCHIVE SHA256]', digest(archive.read_bytes()))
        return 0 if result['status'] == 'STATIC_INSPECTION_COLLECTED' else 2
    except Exception as exc:
        print('[INSPECTION STOPPED]', reason(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
