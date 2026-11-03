"""One content-addressed source/config capsule per workflow, never a production lock.

A stable double scan detects ordinary in-place deploy races; it is NOT a filesystem
atomic snapshot or proof that a multi-file release is semantically complete.
"""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import shutil
import stat
import time
import tokenize
from common import Refuse, atomic_json, sha_bytes, sha_file, read_json, utc
import shadow_copy

MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILES = 12000
SKIP = shadow_copy.SKIP | {'.private', '.snapshots', 'workflows', 'runs', 'diagnostics', '.idea', '.vscode', 'env'}
EXT = shadow_copy.EXT | {'.jinja', '.jinja2', '.html', '.pem'}
# PEM private keys are not copied by this module; explicit local dependency
# certificates may be configured separately, never guessed or exposed publicly.
EXT.discard('.pem')

class CaptureChanged(Refuse):
    pass


def inventory(root: Path) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise Refuse('Snapshot source must be a directory')
    rows = {}; total = 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        retained = []
        for name in sorted(dirs):
            if name in SKIP or name.startswith('.'):
                continue
            p = Path(parent) / name
            if p.is_symlink():
                raise Refuse('Directory symlink requires a stable release source: ' + str(p.relative_to(root)))
            retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            p = Path(parent) / name; rel = p.relative_to(root).as_posix()
            if '.bak' in name or name.endswith(('.orig','.save','~')):
                continue
            if p.suffix not in EXT and not name.startswith('.env'):
                continue
            target = p.resolve(strict=True)
            if not target.is_relative_to(root):
                raise Refuse('External source symlink refused: ' + rel)
            before = target.stat()
            if not stat.S_ISREG(before.st_mode):
                raise Refuse('Nonregular source file: ' + rel)
            if before.st_size > MAX_FILE:
                raise Refuse('Snapshot text asset too large: ' + rel)
            digest = sha_file(target); after = target.stat()
            meta = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if meta(before) != meta(after):
                raise CaptureChanged('Source changed during hash: ' + rel)
            total += after.st_size
            if len(rows) >= MAX_FILES or total > MAX_TOTAL:
                raise Refuse('Source capsule size/count limit exceeded; use an explicit release directory')
            rows[rel] = {'sha256': digest, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
                         'ctime_ns': after.st_ctime_ns, 'inode': after.st_ino, 'device': after.st_dev}
    if 'api/api_server.py' not in rows:
        raise Refuse('Selected source has no api/api_server.py')
    return rows


def content_map(rows: dict) -> dict:
    return {k: v['sha256'] for k, v in rows.items()}


def snapshot_id(manifest: dict) -> str:
    return sha_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())


def compatibility(source: Path) -> dict:
    """Check the entry-module bindings actually consumed by shadow_entry.

    Handler/model *names and their source-file location* are not an HTTP
    contract. APIRouter/factory imports may deliberately keep chat_message and
    ChatRequest out of this module. Published routes and real cookie/payload/
    identity checks are still enforced after the guarded app starts.

    No application import, recursive symbol guessing or historical hash gate
    is performed here. An explicit import is a deferred binding check only,
    not proof that its target exists or is callable.
    """
    p = source / 'api/api_server.py'
    try:
        tree = ast.parse(p.read_text(encoding='utf-8-sig'))
    except (SyntaxError, UnicodeError) as exc:
        raise Refuse('Shadow API syntax is incompatible: ' + type(exc).__name__) from None

    functions = {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    imported = set()
    assigned = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names
                            if alias.name != '*')
        elif isinstance(node, ast.Import):
            imported.update(alias.asname or alias.name.split('.')[0]
                            for alias in node.names)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                assigned.update(n.id for n in ast.walk(target)
                                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assigned.add(node.target.id)

    missing = []
    # The existing identity observer looks up this name on api_module.
    # Keep this requirement; do not fabricate identities or bypass auth.
    if '_chat_user_id' not in functions | imported:
        missing.append('_chat_user_id')
    # The existing ASGI wrapper calls api_module.app. Renamed/absent exports
    # cannot be assumed to work merely because some router exists elsewhere.
    if 'app' not in assigned | imported | functions:
        missing.append('app')
    if missing:
        raise Refuse('Shadow instrumentation contract changed; missing ' + ', '.join(missing))

    def binding(name: str) -> str:
        if name in functions: return 'function'
        if name in classes: return 'class'
        if name in imported: return 'explicit_import'
        if name in assigned: return 'assignment'
        return 'not_declared_at_entry_top_level'

    return {
        'pass': True,
        'required_symbols': ['_chat_user_id', 'app'],
        'observed_entry_bindings': {name: binding(name) for name in
                                    ('_chat_user_id', 'app', 'chat_message', 'ChatRequest')},
        'handler_and_model_location_required': False,
        'http_contract_verified': False,
        'application_imported': False,
        'deferred_checks': ['guarded_application_import', 'dedicated_redis_binding',
                            'published_auth_and_chat_routes', 'live_cookie_and_request_payload',
                            'original_resolver_identity_evidence', 'agent_and_model_smoke'],
        'scope': 'entry bindings only; no claim that HTTP routes, auth or payloads have passed',
    }


def historical_diff(source: Path, reference: dict) -> dict:
    changes = []
    for rel, expected in reference.get('critical_original_sha256',{}).items():
        p = source / rel
        actual = sha_file(p) if p.is_file() else None
        if actual != expected:
            changes.append({'path': rel, 'expected': expected, 'actual': actual})
    return {'historical_match': not changes, 'blocking': False,
            'reference_snapshot': reference.get('snapshot'), 'differences': changes,
            'policy': 'history is provenance, never permission to mutate production'}


def capture(root: Path, dest: Path, configs: dict[str, Path], *, attempts=3,
            settle_seconds=1.0, hook=None, retry_log=None) -> dict:
    """Copy independent bytes, retry only capture races, never retry requests.
    configs is a safe basename -> source path mapping (e.g. proxy-original.yaml).
    """
    if dest.exists(): raise Refuse('Snapshot destination already exists')
    if dest.resolve().is_relative_to(root.resolve()): raise Refuse('Snapshot must be outside live source')
    for attempt in range(1, attempts+1):
        tmp = dest.parent / (dest.name + '.capture-' + str(attempt))
        if tmp.exists(): raise Refuse('Unexpected capture staging directory')
        tmp.mkdir(parents=True, mode=0o700)
        try:
            source_root = root.resolve(strict=True)
            before = inventory(source_root)
            config_before = {}
            for name, path in configs.items():
                if Path(name).name != name or Path(name).is_absolute(): raise Refuse('Invalid capsule config name')
                if not path.is_file() or path.stat().st_size > MAX_FILE: raise Refuse('Missing/oversized capture config')
                data = path.read_bytes(); config_before[name] = sha_bytes(data)
                (tmp/name).write_bytes(data); (tmp/name).chmod(0o600)
            tree = tmp/'source'; tree.mkdir(mode=0o700)
            for rel, info in before.items():
                q = tree/rel; q.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                data = (source_root/rel).read_bytes()
                if sha_bytes(data) != info['sha256']: raise CaptureChanged('Source changed during copy: '+rel)
                q.write_bytes(data); q.chmod(0o600)
                if q.stat().st_ino == (source_root/rel).stat().st_ino and q.stat().st_dev == (source_root/rel).stat().st_dev:
                    raise Refuse('Shadow copy unexpectedly shares inode')
            if hook: hook(attempt, source_root, tmp)
            time.sleep(max(0, settle_seconds))
            after = inventory(source_root)
            if root.resolve(strict=True) != source_root or before != after:
                raise CaptureChanged('Source changed across copy interval')
            for name, path in configs.items():
                if not path.is_file() or sha_file(path) != config_before[name]: raise CaptureChanged('Config changed during copy: '+name)
            entry_contract = compatibility(tree)
            manifest = {'source': content_map(before), 'configs': config_before}
            result = {'snapshot_id': snapshot_id(manifest), 'manifest': manifest,
                      'captured_at': utc(), 'source_root':str(source_root),'capture_attempts':attempt,
                      'consistent_observation':True, 'atomic_filesystem_snapshot':False,
                      'semantic_release_completeness':'NOT_PROVEN', 'source_count':len(before),
                      'source_compatibility':entry_contract}
            atomic_json(tmp/'snapshot.json', result)
            os.replace(tmp, dest)
            # Owner read-only, no external process receives this snapshot as cwd.
            for p in dest.rglob('*'):
                if p.is_file(): p.chmod(0o400)
            return result
        except (CaptureChanged, FileNotFoundError) as exc:
            if retry_log is not None: retry_log.append({'attempt':attempt,'kind':type(exc).__name__,'at':utc()})
            shutil.rmtree(tmp)
            if attempt == attempts: raise Refuse('SNAPSHOT_UNSTABLE: bounded capture retries exhausted; use a release directory') from None
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True); raise
    raise Refuse('No capture attempt')


def verify(capsule: Path) -> dict:
    recorded = read_json(capsule/'snapshot.json'); manifest = recorded['manifest']
    present = {}
    for p in (capsule/'source').rglob('*'):
        if p.is_symlink(): raise Refuse('Snapshot symlink introduced')
        if p.is_file(): present[p.relative_to(capsule/'source').as_posix()] = sha_file(p)
    configs = {}
    for name in manifest['configs']:
        p=capsule/name
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(capsule.resolve()):
            raise Refuse('Frozen config missing or linked outside capsule')
        configs[name]=sha_file(p)
    actual = {'source':present,'configs':configs}
    ok = actual == manifest and snapshot_id(actual) == recorded['snapshot_id']
    if not ok: raise Refuse('FROZEN_SNAPSHOT_CHANGED: refusing mixed-baseline measurements')
    return {'pass':True,'snapshot_id':recorded['snapshot_id']}


def working_copy(capsule: Path, dest: Path, production_roots: list[str], proxy_port: int, report: Path):
    verify(capsule)
    if dest.exists(): raise Refuse('Cell working source exists')
    shutil.copytree(capsule/'source', dest, copy_function=shutil.copyfile)
    changes=[]; syntax_warnings=[]
    for p in dest.rglob('*'):
        if not p.is_file(): continue
        p.chmod(0o600)
        if p.suffix!='.py': continue
        text=p.read_text(encoding='utf-8-sig'); original=text; lines=[]
        try:
            for old in sorted(set(production_roots), key=len, reverse=True):
                text, altered = shadow_copy.rewrite_literals(text, old, str(dest.resolve()), proxy_port)
                lines.extend(altered)
        except (SyntaxError, UnicodeError, tokenize.TokenError):
            if any(old in text for old in production_roots) or ':8003/v1' in text:
                raise Refuse('Cannot safely relocate path-bearing Python: '+str(p.relative_to(dest))) from None
            syntax_warnings.append(p.relative_to(dest).as_posix()); continue
        if text != original:
            p.write_text(text,encoding='utf-8')
            changes.append({'path':p.relative_to(dest).as_posix(),'lines':sorted(set(lines)),
                            'kind':'path_or_proxy_literal_only','sha256':sha_file(p)})
    entry_contract = compatibility(dest)
    atomic_json(report, {'snapshot_id':read_json(capsule/'snapshot.json')['snapshot_id'],
                        'changes':changes,'unparsed_unused_helpers':syntax_warnings,
                        'source_compatibility':entry_contract,
                        'scope':'only a disposable cell copy is relocated'})
