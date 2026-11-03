#!/usr/bin/env python3
"""Shadow backend entry: the shadow copy's ASGI app under uvicorn plus an in-process provenance probe.  SHADOW ONLY.

shadow_stack.py up starts this instead of `python -m uvicorn api.api_server:app`.  Before the app is imported it installs an
audit hook that records files opened under a production tree and .env files opened anywhere.  GET /_shadow/provenance is
answered here, before the app and its middleware, for 127.0.0.1 only, and reports where the running code came from:
  orchestrator   agents.root.orchestrator must be <shadow root>/agents/root/orchestrator.py with the Sys1 overlay active
  prod_modules   modules whose file lies under a production tree
  foreign_app    modules of the copy's own top-level packages (agents, tools, core, app, api, …) loaded from outside the copy
  prod_sys_path  sys.path entries under a production tree
  opened         files opened under a production tree and .env files opened anywhere (paths only, never contents)
  env_added      names (never values) of environment variables that appeared after start (e.g. load_dotenv)
verdict is "ok" only when the first five are clean.  shadow_stack.py up/verify and s3_shadow.py drive refuse otherwise.
The team code hard-codes sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent'); shadowpatch init-shadow rewrites it,
this probe proves the rewrite held in the running process.

  python shadow_entry.py --shadow-root DIR --prod-tree DIR [--prod-tree DIR …] --host 127.0.0.1 --port 8210
  python shadow_entry.py --shadow-root DIR --prod-tree DIR --check-only       # import the app, print the report, exit 0 / 2
Standard library only (plus uvicorn when serving); runs on the backend's Python (3.10+).
"""
from __future__ import annotations

import argparse, hashlib, importlib, json, os, sys, threading

PROBE_PATH = "/_shadow/provenance"
ORCH, OVERLAY = "agents.root.orchestrator", "agents.root.sys1_dispatch"
MAX_OPENED = 200
_trees: list[str] = []                       # normcased absolute and real paths of the production trees
_opened: list[str] = []
_lock = threading.Lock()
_env_at_start = set(os.environ)


def _norm(p) -> str: return os.path.normcase(os.path.abspath(p))


def _under(path, trees) -> bool:
    p = _norm(path)
    return any(p == t or p.startswith(t.rstrip(os.sep) + os.sep) for t in trees)


def _forms(p) -> list[str]: return sorted({_norm(p), os.path.normcase(os.path.realpath(p))})


def _is_env_file(path: str) -> bool:
    n = os.path.basename(path)
    return n == ".env" or (n.startswith(".env.") and not n.endswith(".example"))


def _audit(event, args):
    if event != "open" or not args or args[0] is None or isinstance(args[0], int): return
    try: p = os.fsdecode(args[0])
    except Exception: return
    if not (_is_env_file(p) or _under(p, _trees)): return
    with _lock:
        if p not in _opened and len(_opened) < MAX_OPENED: _opened.append(p)


def install(prod_trees) -> None:
    """Call before the app is imported.  Audit hooks cannot be removed: one per process."""
    _trees[:] = sorted({f for t in prod_trees for f in _forms(t)})
    sys.addaudithook(_audit)


def _file(m):
    f = getattr(m, "__file__", None)
    return os.path.normpath(f) if isinstance(f, str) else None       # the rewritten literal is posix-style on Windows


def _app_packages(root: str) -> set[str]:
    out = set()
    for e in os.scandir(root):
        if e.is_file() and e.name.endswith(".py"): out.add(e.name[:-3])
        elif e.is_dir() and not e.name.startswith(".") and any(x.endswith(".py") for x in os.listdir(e.path)): out.add(e.name)
    return out - set(getattr(sys, "stdlib_module_names", ()))


def report(shadow_root: str) -> dict:
    root = _forms(shadow_root)
    loaded_before = ORCH in sys.modules
    try: orch, orch_err = importlib.import_module(ORCH), None     # the module chat requests use (module cache)
    except Exception as e: orch, orch_err = None, f"{type(e).__name__}: {e}"[:300]
    of = _file(orch); expect = os.path.join(shadow_root, "agents", "root", "orchestrator.py")
    ov = sys.modules.get(OVERLAY); ovf = _file(ov)
    overlay_active = orch is not None and ov is not None and getattr(orch, "sys1_dispatch", None) is ov
    try: overlay_enabled = bool(ov._enabled_here()) if ov is not None else False   # marker/pkg_root/prod_roots guard of the payload
    except Exception: overlay_enabled = False
    mods = [(n, _file(m)) for n, m in list(sys.modules.items())]
    pkgs = _app_packages(shadow_root)
    prod_modules = sorted(n for n, f in mods if f and _under(f, _trees))
    foreign_app = sorted(n for n, f in mods if f and n.split(".")[0] in pkgs and not _under(f, root))
    prod_sys_path = [p for p in sys.path if p and _under(p, _trees)]
    with _lock: opened = list(_opened)
    problems = []
    if not of or _norm(os.path.realpath(of)) != _norm(os.path.realpath(expect)): problems.append(f"orchestrator not from the shadow copy: {of or orch_err}")
    if not overlay_active: problems.append("Sys1 overlay not active in the loaded orchestrator")
    elif not (ovf and _under(ovf, root)): problems.append(f"Sys1 overlay module outside the shadow copy: {ovf}")
    elif not overlay_enabled: problems.append("Sys1 overlay inert (marker pkg_root / prod_roots guard)")
    if prod_modules: problems.append(f"{len(prod_modules)} modules from a production tree")
    if foreign_app: problems.append(f"{len(foreign_app)} app modules from outside the shadow copy")
    if prod_sys_path: problems.append(f"sys.path has production entries: {prod_sys_path}")
    if opened: problems.append(f"{len(opened)} production or .env files opened")
    sha = None
    if of and os.path.isfile(of):
        with open(of, "rb") as f: sha = hashlib.sha256(f.read()).hexdigest()
    return {"verdict": "ok" if not problems else "FAIL", "problems": problems, "shadow_root": shadow_root, "prod_trees": list(_trees),
            "orchestrator": {"file": of, "sha256": sha, "loaded_before_probe": loaded_before, "overlay_active": overlay_active,
                             "overlay_file": ovf, "overlay_enabled": overlay_enabled},
            "prod_modules": prod_modules[:50], "foreign_app": foreign_app[:50], "prod_sys_path": prod_sys_path, "opened": opened,
            "env_added": sorted(set(os.environ) - _env_at_start), "modules": len(mods), "pid": os.getpid(), "python": sys.executable}


class Probe:
    """Answers PROBE_PATH itself, ahead of the app's middleware (no auth, CORS or rate limits involved); the rest goes to the app."""
    def __init__(self, app, shadow_root): self.app, self.root = app, shadow_root

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == PROBE_PATH:
            local = (scope.get("client") or ("",))[0] in ("127.0.0.1", "::1")
            code, body = (200, report(self.root)) if local else (403, {"detail": "local only"})
            b = json.dumps(body, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": code,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(b)).encode())]})
            await send({"type": "http.response.body", "body": b})
            return
        await self.app(scope, receive, send)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shadow-root", required=True); ap.add_argument("--prod-tree", action="append", required=True)
    ap.add_argument("--app", default="api.api_server:app"); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8210)
    ap.add_argument("--check-only", action="store_true", help="import the app, print the provenance report, exit 0 (ok) / 2")
    a = ap.parse_args(argv)
    root = os.path.realpath(a.shadow_root)
    install(a.prod_tree)
    sys.path[0:1] = [root]          # replace this script's directory: the copy comes first, as with `python -m uvicorn` run in it
    os.chdir(root)
    mod, _, attr = a.app.partition(":")
    try: app = getattr(importlib.import_module(mod), attr)
    except Exception as e:
        if not a.check_only: raise
        print(f"[shadow_entry] importing {a.app} failed: {type(e).__name__}: {e}", file=sys.stderr); app = None
    if a.check_only:
        r = report(root); print(json.dumps(r, ensure_ascii=False, indent=1)); return 0 if r["verdict"] == "ok" else 2
    import uvicorn
    uvicorn.run(Probe(app, root), host=a.host, port=a.port, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
