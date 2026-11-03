"""Observe actual resolver returns before routers capture the callable.

Only in-memory code loaded from the already isolated Shadow source tree is
instrumented. No file is rewritten, no credential is inspected, and no second
identity lookup is performed. The existing import-origin guard is consulted
first. There is no sys.settrace/setprofile hook or per-call global tracing.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import functools
import hashlib
import importlib.abc
import importlib.machinery
import inspect
from pathlib import Path
import sys
import types

from common import Refuse, atomic_json, event
from shadow_guard import TRACE_ID

_BINDING = '_chat_user_id'
_HELPER = '__blink_shadow_observe_identity_binding__'
_OWNER = '__blink_shadow_identity_observer__'


def _declares_resolver(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return statement.name == _BINDING
    if isinstance(statement, (ast.ImportFrom, ast.Import)):
        return any((alias.asname or (alias.name.split('.')[0]
                   if isinstance(statement, ast.Import) else alias.name)) == _BINDING
                   for alias in statement.names)
    return False


def instrument_source(source: str, filename: str, module: str):
    """Wrap only explicit module-top-level definitions/import bindings.

    Existing decorators execute first, in their original order. Function bodies,
    router definitions, HTTP handlers, signatures and auth results are not
    rewritten. Other layouts are NOT guessed; runtime evidence must still exist.
    """
    tree = ast.parse(source, filename=filename)
    targets = [statement for statement in tree.body if _declares_resolver(statement)]
    if not targets:
        return None, []
    if any(isinstance(n, ast.Name) and n.id == _HELPER for n in ast.walk(tree)):
        raise Refuse('Reserved Shadow identity-observer name already used')
    result, sites = [], []
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
    for statement in tree.body:
        result.append(statement)
        if not _declares_resolver(statement):
            continue
        kind = 'definition' if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) else 'explicit_import'
        args = [ast.Name(id=_BINDING, ctx=ast.Load()),
                ast.Constant(value=module), ast.Constant(value=filename),
                ast.Constant(value=kind), ast.Constant(value=digest)]
        assignment = ast.Assign(
            targets=[ast.Name(id=_BINDING, ctx=ast.Store())],
            value=ast.Call(func=ast.Name(id=_HELPER, ctx=ast.Load()), args=args, keywords=[]))
        ast.copy_location(assignment, statement)
        result.append(assignment)
        sites.append({'line': statement.lineno, 'binding_kind': kind})
    tree.body = result
    ast.fix_missing_locations(tree)
    return compile(tree, filename, 'exec', dont_inherit=True), sites


class IdentityObserver:
    def __init__(self, shadow_root: Path, evidence_dir: Path, *, snapshot_id=None,
                 trace_id=TRACE_ID, emit=event):
        self.root = Path(shadow_root).resolve(strict=True)
        self.evidence = Path(evidence_dir)
        self.snapshot_id = snapshot_id
        self.trace_id = trace_id
        self.emit = emit
        self.bindings = []
        self.wrappers = {}  # retain originals; object identity cannot be recycled

    def _inside(self, filename: str) -> Path:
        path = Path(filename).resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise Refuse('Identity resolver resolved outside private Shadow source')
        return path

    def _record(self, uid):
        # Only the ORIGINAL return value is used. Never arguments/cookies/body.
        text = str(uid)
        self.emit(self.evidence / 'auth-resolution.jsonl', 'chat_identity_resolved',
                  request_id=self.trace_id.get(),
                  auth_mode='guest' if text.startswith(('guest-', 'guest_')) else 'member',
                  identity_fingerprint=hashlib.sha256(
                      ('blink-test-member:' + text).encode()).hexdigest()[:20],
                  source='original_backend_identity_resolver',
                  observation_hook='pre_router_binding', snapshot_id=self.snapshot_id)

    def bind(self, resolver, module, filename, kind, source_text_sha256):
        origin = self._inside(filename)
        owner = getattr(resolver, _OWNER, None)
        if owner is self:
            wrapped = resolver  # re-export of the same observer: no double event
        else:
            if owner is not None:
                raise Refuse('Resolver is already owned by another identity observer')
            if not inspect.isfunction(resolver):
                raise Refuse('Identity resolver is not a supported Python function')
            self._inside(resolver.__code__.co_filename)
            if inspect.isgeneratorfunction(resolver) or inspect.isasyncgenfunction(resolver):
                raise Refuse('Generator identity resolver is not supported')
            wrapped = self.wrappers.get(resolver)
            if wrapped is None:
                if inspect.iscoroutinefunction(resolver):
                    async def call(*args, **kwargs):
                        uid = await resolver(*args, **kwargs)
                        self._record(uid)
                        return uid
                else:
                    def call(*args, **kwargs):
                        uid = resolver(*args, **kwargs)
                        self._record(uid)
                        return uid
                # Keep the ORIGINAL globals for forward-reference annotations
                # (e.g. Request) used by FastAPI/inspect; closures hold our helper.
                wrapped = types.FunctionType(call.__code__, resolver.__globals__,
                                             call.__name__, call.__defaults__, call.__closure__)
                functools.update_wrapper(wrapped, resolver)
                setattr(wrapped, _OWNER, self)
                self.wrappers[resolver] = wrapped
        self.bindings.append({'module': module,
                              'relative_file': origin.relative_to(self.root).as_posix(),
                              'binding_kind': kind,
                              'source_text_sha256': source_text_sha256,
                              'async_resolver': inspect.iscoroutinefunction(wrapped)})
        return wrapped

    def assert_export(self, resolver):
        if getattr(resolver, _OWNER, None) is not self:
            raise Refuse('Shadow exported resolver was not observed before router assembly')
        atomic_json(self.evidence / 'identity-observer-proof.json', {
            'installed': True,
            'snapshot_id': self.snapshot_id,
            'hook': 'in_memory_explicit_binding_before_router_assembly',
            'bindings': self.bindings,
            'distinct_wrappers': len(self.wrappers),
            'production_files_written': 0,
            'shadow_files_written': 0,
            'additional_auth_lookups': 0,
            'actual_request_identity_verified': False,
            'scope': 'installation evidence only; per-request original returns still required'})


class _ObservedLoader(importlib.abc.Loader):
    def __init__(self, original, observer, fullname, origin):
        self.original, self.observer = original, observer
        self.fullname, self.origin = fullname, origin

    def create_module(self, spec):
        return self.original.create_module(spec)

    def exec_module(self, module):
        # Read the captured source, not stale .pyc; never write a bytecode file.
        source = self.original.get_source(self.fullname)
        if source is None:
            raise Refuse('Private Shadow module source is unavailable')
        code, _ = instrument_source(source, self.origin, self.fullname)
        if code is None:
            code = compile(source, self.origin, 'exec', dont_inherit=True)
        if _HELPER in module.__dict__:
            raise Refuse('Identity-observer helper collision')
        module.__dict__[_HELPER] = self.observer.bind
        try:
            exec(code, module.__dict__)
        finally:
            module.__dict__.pop(_HELPER, None)

    def __getattr__(self, name):
        return getattr(self.original, name)


class _ObservedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, observer, source_guard):
        self.observer, self.source_guard = observer, source_guard

    def find_spec(self, fullname, path=None, target=None):
        # Delegation is essential: do not bypass the production-import guard.
        spec = self.source_guard.find_spec(fullname, path, target)
        if spec is None:
            return None
        if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            self.observer._inside(spec.origin)
            spec.loader = _ObservedLoader(spec.loader, self.observer, fullname, spec.origin)
        return spec


@contextmanager
def observe_identity_imports(shadow_root: Path, evidence_dir: Path, *, snapshot_id=None,
                             trace_id=TRACE_ID, emit=event):
    from shadow_guard import SourceFinder
    root = Path(shadow_root).resolve(strict=True)
    if 'api.api_server' in sys.modules:
        raise Refuse('API already imported before identity observation was installed')
    candidates = [f for f in sys.meta_path if type(f) is SourceFinder and f.root == root]
    if len(candidates) != 1:
        raise Refuse('Expected one active private Shadow import-origin guard')
    observer = IdentityObserver(root, evidence_dir, snapshot_id=snapshot_id,
                                trace_id=trace_id, emit=emit)
    finder = _ObservedFinder(observer, candidates[0])
    sys.meta_path.insert(0, finder)
    try:
        yield observer
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
