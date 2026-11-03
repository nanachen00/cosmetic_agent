#!/usr/bin/env python3
"""Isolated shadow backend for S3 on the restored MI300X platform (2026-10-21 layout).  SHADOW ONLY.

Replaces the pre-incident Gate2 harness route (bench.py needs a LiteLLM proxy config, MANIFEST.sha256 and 16 member
accounts, none of which exist after the 2026-10-20 container rebuild).  Starts ONE uvicorn of the shadow copy:

  shadow/cosmetic-agent (shadowpatch init-shadow + apply)  ->  127.0.0.1:8210  (shadow_entry.py: uvicorn + provenance probe)
    code       : the copy only.  init-shadow points the team's hard-coded /var/lib/jenkins/tomod/cosmetic-agent at the copy;
                 after /readyz, `up` asks the backend process where its orchestrator and every module came from and which
                 .env files it opened (GET /_shadow/provenance) and stops the stack unless the verdict is ok
    PostgreSQL : own database blinkai_shadow_* on the local server (schema = production migration 0001)
    Redis      : own redis-server on 127.0.0.1:6380, password in a 0600 config file, no persistence
    env        : production .env values + overrides (DB, Redis, release tag, overlay control); SMTP, admin list,
                 Better Stack, Firecrawl and OTEL export are blanked so the shadow never mails, administers or reports
    shared     : Neo4j Aura (read), Zep Cloud (guest memory writes), OpenAI (legacy LLM arm), TAIDE gateway :8000 (GPU)
                 -> `up` refuses without --ack-shared-data-services

  python3 shadow_stack.py plan                                    # no side effects; prints settings and checks
  python3 shadow_stack.py env-check                               # shadow env keys: 已填/空白/覆寫 (never values)
  python3 shadow_stack.py up --ack-shared-data-services           # create DB/schema, start redis + backend, wait /readyz, provenance
  python3 shadow_stack.py verify                                  # provenance of the running backend again (s3_shadow drive does too)
  python3 shadow_stack.py status
  python3 shadow_stack.py down [--drop-db --yes]                  # SIGTERM only the processes this tool started

Never touches production ports/processes/files: refuses protected ports, a shadow root inside a production root
(shadowpatch G1/G2), a database name that is not blinkai_shadow_*, and processes whose command line it did not record.
"""
from __future__ import annotations

import argparse, json, os, re, secrets, shlex, shutil, signal, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROTECTED_PORTS = {5432, 6389, 8000, 8001, 8002, 8003, 8006, 8010, 8200, 8201}
BACKEND_PORTS = range(8210, 8220)
REDIS_PORTS = range(6380, 6389)
DB_RE = re.compile(r"^blinkai_shadow_[a-z0-9_]{1,40}$")
PASSTHROUGH = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "LD_LIBRARY_PATH", "TMPDIR",
               # Windows (local replica): the interpreter and sockets need these
               "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"}
WINDOWS = os.name == "nt"
BLANKED = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL", "ADMIN_USER_IDS", "FIRECRAWL_API_KEY",
           "BETTER_STACK_ERRORS_DSN", "BETTER_STACK_LOG_SOURCE_TOKEN", "BETTER_STACK_LOG_INGESTING_HOST",
           "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")


class Refuse(SystemExit):
    def __init__(self, msg): super().__init__(f"[REFUSED] {msg}")


# ── pure helpers (unit-tested on any OS) ───────────────────────────────────────────────────────────────────────────────
def parse_dotenv(text: str) -> dict:
    """KEY=VALUE lines; `export ` prefix; '#' comment lines; quoted values; unquoted values end at ' #' (as .env.example)."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("export "): line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k): continue
        v = v.strip()
        if v[:1] in ("'", '"') and v.find(v[0], 1) > 0: v = v[1:v.find(v[0], 1)]
        else: v = re.split(r"\s+#", v, maxsplit=1)[0].strip() if not v.startswith("#") else ""
        out[k] = v
    return out


def shadow_db_url(prod_url: str, db: str) -> str:
    if not DB_RE.match(db): raise Refuse(f"database name must match {DB_RE.pattern}: {db!r}")
    u = urllib.parse.urlsplit(prod_url)
    if not u.scheme.startswith("postgres") or not u.hostname: raise Refuse("production DATABASE_URL is not a postgres URL")
    if u.path.lstrip("/") == db: raise Refuse("shadow database name equals the production database")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, "/" + db, u.query, u.fragment))


def redact_url(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    if u.password is None: return url
    netloc = f"{u.username}:***@{u.hostname}" + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def check_ports(backend: int, redis: int):
    for name, port, allowed in (("backend", backend, BACKEND_PORTS), ("redis", redis, REDIS_PORTS)):
        if port in PROTECTED_PORTS: raise Refuse(f"{name} port {port} is a production port")
        if port not in allowed: raise Refuse(f"{name} port {port} outside {allowed.start}-{allowed.stop - 1}")


def compose_env(prod: dict, *, db_url: str, redis_url: str, release: str, control: str, prom_dir: str) -> tuple[dict, dict]:
    """(shadow env, override map).  Production values are kept except the isolated / blanked keys."""
    if not prod.get("DATABASE_URL"): raise Refuse("production .env has no DATABASE_URL")
    over = {"DATABASE_URL": db_url, "REDIS_URL": redis_url, "APP_ENV": "shadow", "APP_RELEASE": release,
            "SYS1_DISPATCH_CONTROL": control, "PROMETHEUS_MULTIPROC_DIR": prom_dir, "OTEL_TRACING_ENABLED": "0"}
    over.update({k: "" for k in BLANKED})
    env = dict(prod); env.update(over)
    if env["DATABASE_URL"] == prod["DATABASE_URL"]: raise Refuse("shadow DATABASE_URL equals production")
    if prod.get("REDIS_URL") and env["REDIS_URL"] == prod["REDIS_URL"]: raise Refuse("shadow REDIS_URL equals production")
    return env, over


def env_report(prod: dict, env: dict, over: dict) -> list[str]:
    rows = []
    for k in sorted(env):
        state = "覆寫" if k in over else ("已填" if env[k] else "空白")
        if k in over and k in ("DATABASE_URL", "REDIS_URL"): state += f" → {redact_url(env[k])}"
        elif k in over and k not in BLANKED and k not in ("SYS1_DISPATCH_CONTROL", "PROMETHEUS_MULTIPROC_DIR"): state += f" → {env[k]}"
        rows.append(f"  {k:36s} {state}")
    return rows


# ── settings / checks ──────────────────────────────────────────────────────────────────────────────────────────────────
def prod_trees(s: dict) -> list[str]:
    """Every tree the shadow backend must not load code or .env from: shadowpatch's production roots plus --prod-root."""
    import shadowpatch as SP
    return sorted({str(p) for p in SP.prod_roots()} | {str(s["prod_root"].resolve())})


def env_files_above(root: Path) -> list[str]:
    """python-dotenv's load_dotenv() without a path walks up from the calling module: a .env above the copy would be loaded."""
    return [str(p / ".env") for p in root.resolve().parents if (p / ".env").is_file()]


def rewrite_state(root: Path) -> str:
    import shadowpatch as SP
    try: rw = json.loads((root / SP.MARKER).read_text(encoding="utf-8"))["rewrite"]
    except Exception: raise Refuse(f"{root / SP.MARKER} has no rewrite record: copy made before r6 — delete it and run init-shadow again")
    left = SP.residual_literals(root)
    if left: raise Refuse(f"{len(left)} .py files still name the production tree ({', '.join(left[:5])}) — init-shadow again")
    return f"{len(rw['files'])} files rewritten -> {rw['to']}"


def settings(a) -> dict:
    prod_root = Path(a.prod_root); state = Path(a.state_dir)
    return {"prod_root": prod_root, "prod_env": Path(a.prod_env or prod_root / ".env"), "shadow_root": Path(a.shadow_root),
            "python": a.backend_python, "port": a.port, "redis_port": a.redis_port, "db": a.db, "control": Path(a.control).resolve(),
            "pg_admin_url": a.pg_admin_url or os.environ.get("SHADOW_PG_ADMIN_URL", ""),
            "state": state, "state_file": state / "stack.json",
            "migration": Path(a.migration or prod_root / "database/postgres/migrations/0001_reconstructed_schema.sql")}


def checks(s: dict) -> list[tuple[str, bool, str]]:
    import shadowpatch as SP
    out = []
    def add(name, fn):
        try: out.append((name, True, fn() or ""))
        except SystemExit as e: out.append((name, False, str(e)))
        except Exception as e: out.append((name, False, f"{type(e).__name__}: {e}"))
    add("ports", lambda: check_ports(s["port"], s["redis_port"]))
    add("db name", lambda: _need(DB_RE.match(s["db"]), f"bad db name {s['db']!r}"))
    add("shadow root (G1/G2)", lambda: str(SP.check_shadow_root(s["shadow_root"])))
    add("overlay applied", lambda: _need((s["shadow_root"] / ".sys1-shadow-overlay").is_file(),
                                         "no .sys1-shadow-overlay marker in the shadow root (run shadowpatch apply)"))
    add("no .env in shadow root", lambda: _need(not any(s["shadow_root"].glob(".env*")), ".env* found in shadow root"))
    add("production paths rewritten", lambda: rewrite_state(s["shadow_root"]))
    add("no .env above shadow root", lambda: _need(not env_files_above(s["shadow_root"]),
                                                   f"{env_files_above(s['shadow_root'])} would be found by load_dotenv(): move the shadow root"))
    add("entry script", lambda: _need((HERE / "shadow_entry.py").is_file(), "shadow_entry.py missing"))
    add("production .env readable", lambda: _need(s["prod_env"].is_file(), f"{s['prod_env']} missing"))
    add("migration present", lambda: _need(s["migration"].is_file(), f"{s['migration']} missing"))
    add("control file", lambda: _need(s["control"].is_file(), f"{s['control']} missing"))
    add("backend python", lambda: _need(Path(s["python"]).is_file(), f"{s['python']} missing"))
    add("postgres admin", lambda: _need(s["pg_admin_url"] or not WINDOWS, "Windows needs --pg-admin-url / SHADOW_PG_ADMIN_URL (no `su postgres`)"))
    for tool in ("redis-server", "psql") + (() if s["pg_admin_url"] else ("createdb",)):
        add(tool, lambda t=tool: _need(_which(t), f"{t} not found (set PG_BIN / REDIS_SERVER / PATH)"))
    return out


def _need(ok, msg):
    if not ok: raise Refuse(msg)
    return ""


def _which(name):
    if name == "redis-server" and os.environ.get("REDIS_SERVER") and Path(os.environ["REDIS_SERVER"]).is_file(): return os.environ["REDIS_SERVER"]
    pg = os.environ.get("PG_BIN")
    for cand in ((Path(pg) / name, Path(pg) / f"{name}.exe") if pg else ()):
        if cand.is_file(): return str(cand)
    found = shutil.which(name)
    if found: return found
    for p in sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"), reverse=True): return str(p)
    return None


def _pg_admin(s, sql: str, db: str | None = None) -> str:
    """Run one SQL statement as the PostgreSQL superuser: `su postgres` on the VM, an admin URL elsewhere (local replica)."""
    if not s["pg_admin_url"]:
        return _run(["su", "postgres", "-c", f"{_which('psql')} {('-d ' + db + ' ') if db else ''}-tAc {shlex.quote(sql)}"])
    u = urllib.parse.urlsplit(s["pg_admin_url"])
    nopw = urllib.parse.urlunsplit((u.scheme, f"{u.username}@{u.hostname}" + (f":{u.port}" if u.port else ""), f"/{db}" if db else u.path, u.query, ""))
    return _run([_which("psql"), "-d", nopw, "-tAc", sql], env={**_base_env(), "PGPASSWORD": u.password or ""})   # -d: Windows psql stops parsing options at a bare argument


def _spawn_kw() -> dict:
    if WINDOWS: return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r: return r.status, json.loads(r.read() or b"{}")


def _listening(port) -> bool:
    import socket
    with socket.socket() as so:
        so.settimeout(0.5)
        return so.connect_ex(("127.0.0.1", port)) == 0


def _cmdline(pid) -> str:
    try: return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError: pass
    try:
        import psutil                                    # Windows / no /proc (local replica)
        return " ".join(psutil.Process(pid).cmdline())
    except Exception: return ""


# ── commands ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def cmd_plan(a):
    s = settings(a); bad = 0
    print(f"shadow root   {s['shadow_root']}\nproduction    {s['prod_root']} (.env read-only: {s['prod_env']})\n"
          f"backend       127.0.0.1:{s['port']}  python {s['python']}  (shadow_entry.py; provenance must be ok after /readyz)\n"
          f"prod trees    {', '.join(prod_trees(s))}\nredis         127.0.0.1:{s['redis_port']} (own instance, no persistence)\n"
          f"database      {s['db']} (schema: {s['migration']})\ncontrol       {s['control']}\nstate         {s['state']}")
    for name, ok, msg in checks(s):
        bad += not ok; print(f"  [{'OK' if ok else 'NG'}] {name}{(': ' + msg) if msg else ''}")
    print("shared (need --ack-shared-data-services): Neo4j Aura read, Zep Cloud guest memory, OpenAI legacy arm, TAIDE :8000")
    return 0 if not bad else 3


def _shadow_env(s, prod, redis_pw=None):
    return compose_env(prod, db_url=shadow_db_url(prod.get("DATABASE_URL", ""), s["db"]),
                       redis_url=f"redis://:{redis_pw or '<generated-at-up>'}@127.0.0.1:{s['redis_port']}/0",
                       release=f"shadow-sys1-{time.strftime('%Y%m%d-%H%M%S')}", control=str(s["control"]), prom_dir=str(s["state"] / "prometheus"))


def cmd_env_check(a):
    s = settings(a); prod = parse_dotenv(s["prod_env"].read_text(encoding="utf-8"))
    env, over = _shadow_env(s, prod)
    print("\n".join(env_report(prod, env, over))); return 0


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode: raise Refuse(f"{shlex.join(cmd[:3])}… failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def cmd_up(a):
    if not a.ack_shared_data_services:
        raise Refuse("--ack-shared-data-services required (Neo4j Aura read, Zep Cloud guest memory writes, OpenAI legacy arm, TAIDE :8000 GPU)")
    s = settings(a)
    failed = [f"{n}: {m}" for n, ok, m in checks(s) if not ok]
    if failed: raise Refuse("pre-checks failed:\n  " + "\n  ".join(failed))
    if s["state_file"].is_file() and json.loads(s["state_file"].read_text()).get("running"): raise Refuse("stack already up (run down first)")
    for p in (s["port"], s["redis_port"]):
        if _listening(p): raise Refuse(f"port {p} already in use")
    prod = parse_dotenv(s["prod_env"].read_text(encoding="utf-8"))
    pw = secrets.token_urlsafe(24); env, _ = _shadow_env(s, prod, pw)
    st = s["state"]; st.mkdir(parents=True, exist_ok=True); os.chmod(st, 0o700); (st / "prometheus").mkdir(exist_ok=True)

    # 1. isolated database + schema (production migration, idempotent)
    u = urllib.parse.urlsplit(env["DATABASE_URL"]); owner = u.username
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", owner or ""): raise Refuse(f"unexpected database owner name {owner!r}")
    exists = _pg_admin(s, f"SELECT 1 FROM pg_database WHERE datname='{s['db']}'").strip() == "1"
    if not exists: _pg_admin(s, f'CREATE DATABASE {s["db"]} OWNER {owner}')
    _pg_admin(s, "CREATE EXTENSION IF NOT EXISTS pgcrypto", db=s["db"])     # same as 60_init_database.sh for the production db
    nopw = urllib.parse.urlunsplit((u.scheme, f"{u.username}@{u.hostname}" + (f":{u.port}" if u.port else ""), u.path, u.query, ""))
    _run([_which("psql"), "-d", nopw, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(s["migration"])], env={**_base_env(), "PGPASSWORD": u.password or ""})
    print(f"[up] database {s['db']} {'reused' if exists else 'created'}; schema applied")

    # 2. own redis (password only in a 0600 config file, never on the command line)
    conf = st / f"redis-shadow-{s['redis_port']}.conf"; (st / "redis").mkdir(exist_ok=True)   # unique name: `down` matches it on the command line
    conf.write_text("\n".join([f"port {s['redis_port']}", "bind 127.0.0.1", "protected-mode yes", f"requirepass {pw}",
                               f'dir "{(st / "redis").as_posix()}"', 'save ""', "appendonly no", "daemonize no"]) + "\n"); os.chmod(conf, 0o600)
    rlog = open(st / "redis.log", "ab")
    # relative to cwd: MSYS2/Cygwin redis builds (local replica) treat any path not starting with "/" as relative
    redis = subprocess.Popen([_which("redis-server"), conf.name], cwd=st, stdout=rlog, stderr=subprocess.STDOUT, **_spawn_kw())

    # 3. backend (env built here; the shadow copy has no .env) via shadow_entry.py: same app and uvicorn settings, plus the probe
    blog = open(st / "backend.log", "ab")
    cmd = [s["python"], str(HERE / "shadow_entry.py"), "--shadow-root", str(s["shadow_root"].resolve())]
    for t in prod_trees(s): cmd += ["--prod-tree", t]
    back = subprocess.Popen(cmd + ["--host", "127.0.0.1", "--port", str(s["port"])],
                            cwd=s["shadow_root"], env={**_base_env(), **env, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",   # logs print Chinese; Windows stdio would be cp932
                                 "PYTHONDONTWRITEBYTECODE": "1"},          # even a leaked production import writes no __pycache__ there
                            stdout=blog, stderr=subprocess.STDOUT, **_spawn_kw())
    state = {"running": True, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "db": s["db"], "port": s["port"], "redis_port": s["redis_port"],
             "shadow_root": str(s["shadow_root"]), "release": env["APP_RELEASE"],
             "procs": {"redis": {"pid": redis.pid, "match": conf.name}, "backend": {"pid": back.pid, "match": f"--port {s['port']}"}}}
    s["state_file"].write_text(json.dumps(state, indent=1)); os.chmod(s["state_file"], 0o600)
    deadline = time.time() + a.ready_timeout
    while time.time() < deadline:
        if back.poll() is not None: raise Refuse(f"backend exited rc={back.returncode}; see {st / 'backend.log'}")
        try:
            code, body = _http_json(f"http://127.0.0.1:{s['port']}/readyz")
            print(f"[up] /readyz {code}: " + json.dumps({k: v for k, v in body.items() if k != 'release'}, ensure_ascii=False)[:400]); break
        except (urllib.error.URLError, OSError, ValueError): time.sleep(2)
    else:
        raise Refuse(f"backend not ready within {a.ready_timeout}s; see {st / 'backend.log'} (stack left up: run down)")

    # 4. provenance: refuse (and stop what we started) unless the running backend is the patched copy and nothing else
    ok, lines = provenance(s, s["port"])
    print("\n".join(lines))
    if not ok:
        _stop(state); state["running"] = False; s["state_file"].write_text(json.dumps(state, indent=1))
        raise Refuse(f"shadow backend provenance FAIL — stopped backend and redis (DB kept); report {st / 'provenance.json'}")
    return 0


def check_provenance(rep: dict, shadow_root: Path) -> list[str]:
    """Problems with a /_shadow/provenance report; the expected orchestrator path is checked here too, not only in the backend."""
    probs = list(rep.get("problems") or [])
    if rep.get("verdict") != "ok" and not probs: probs.append(f"verdict {rep.get('verdict')!r}")
    f = (rep.get("orchestrator") or {}).get("file")
    want = (shadow_root.resolve() / "agents" / "root" / "orchestrator.py")
    if not f or Path(f).resolve() != want: probs.append(f"orchestrator {f} is not {want}")
    return sorted(set(probs), key=probs.index)


def provenance(s: dict, port: int) -> tuple[bool, list[str]]:
    """GET the probe, keep the report (0600) in the state dir, and summarise it; (ok, printable lines)."""
    try: _, rep = _http_json(f"http://127.0.0.1:{port}/_shadow/provenance", timeout=30)
    except Exception as e: rep = {"verdict": "UNREACHABLE", "problems": [f"probe unreachable: {type(e).__name__}: {e}"[:300]]}
    out = s["state"] / "provenance.json"; out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8"); os.chmod(out, 0o600)
    probs = check_provenance(rep, s["shadow_root"]); o = rep.get("orchestrator") or {}
    lines = [f"[provenance] {'ok' if not probs else 'FAIL'}: orchestrator {o.get('file')} (sha256 {str(o.get('sha256'))[:12]}…, "
             f"overlay active={o.get('overlay_active')}, enabled={o.get('overlay_enabled')}); {rep.get('modules')} modules, "
             f"{len(rep.get('prod_modules') or [])} from production, {len(rep.get('opened') or [])} production/.env files opened",
             f"[provenance] env vars added after start (names only): {rep.get('env_added') or 'none'}"]
    lines += [f"[provenance]   ✗ {p}" for p in probs]
    lines += [f"[provenance]     module {m}" for m in (rep.get("prod_modules") or [])[:10]]
    lines += [f"[provenance]     opened {p}" for p in (rep.get("opened") or [])[:10]]
    return not probs, lines


def _base_env():
    return {k: v for k, v in os.environ.items() if k in PASSTHROUGH or k.startswith("LC_")}


def cmd_status(a):
    s = settings(a)
    if not s["state_file"].is_file(): print("not started"); return 1
    st = json.loads(s["state_file"].read_text())
    for name, p in st.get("procs", {}).items():
        cl = _cmdline(p["pid"]); print(f"  {name:8s} pid {p['pid']}: {'alive' if p['match'] in cl else 'gone'}")
    try: print("  /readyz", *_http_json(f"http://127.0.0.1:{st['port']}/readyz"))
    except Exception as e: print(f"  /readyz unreachable: {type(e).__name__}")
    return 0


def cmd_verify(a):
    s = settings(a)
    if not s["state_file"].is_file() or not json.loads(s["state_file"].read_text()).get("running"): print("not started"); return 1
    ok, lines = provenance(s, json.loads(s["state_file"].read_text())["port"]); print("\n".join(lines)); return 0 if ok else 4


def _stop(st: dict):
    """SIGTERM only the processes recorded at `up`, after checking their command lines."""
    for name in ("backend", "redis"):
        p = st.get("procs", {}).get(name)
        if not p: continue
        if p["match"] not in _cmdline(p["pid"]): print(f"  {name}: pid {p['pid']} not ours / gone — skipped"); continue
        os.kill(p["pid"], signal.SIGTERM)
        for _ in range(60):
            if p["match"] not in _cmdline(p["pid"]): break
            time.sleep(0.5)
        print(f"  {name}: {'stopped' if p['match'] not in _cmdline(p['pid']) else 'STILL RUNNING (no SIGKILL; check manually)'}")


def cmd_down(a):
    s = settings(a)
    if not s["state_file"].is_file(): print("nothing to stop"); return 0
    st = json.loads(s["state_file"].read_text())
    _stop(st)
    if a.drop_db:
        if not a.yes: raise Refuse("--drop-db needs --yes")
        if not DB_RE.match(st["db"]): raise Refuse("recorded db name is not a shadow db")
        _pg_admin(s, f"DROP DATABASE IF EXISTS {st['db']}"); print(f"  database {st['db']} dropped")
    st["running"] = False; s["state_file"].write_text(json.dumps(st, indent=1)); return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prod-root", default="/var/lib/jenkins/tomod/cosmetic-agent"); ap.add_argument("--prod-env")
    ap.add_argument("--shadow-root", default=str(HERE / "shadow" / "cosmetic-agent"))
    ap.add_argument("--backend-python", default="/opt/conda/envs/blink-backend/bin/python")
    ap.add_argument("--port", type=int, default=8210); ap.add_argument("--redis-port", type=int, default=6380)
    ap.add_argument("--db", default="blinkai_shadow_sys1"); ap.add_argument("--migration")
    ap.add_argument("--control", default=str(HERE / "control" / "sys1_dispatch.json"))
    ap.add_argument("--state-dir", default=str(HERE / "runs" / "stack"))
    ap.add_argument("--pg-admin-url", help="superuser URL instead of `su postgres` (local replica on Windows); or SHADOW_PG_ADMIN_URL")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("plan"); sp.add_parser("env-check"); sp.add_parser("status"); sp.add_parser("verify")
    u = sp.add_parser("up"); u.add_argument("--ack-shared-data-services", action="store_true"); u.add_argument("--ready-timeout", type=int, default=180)
    d = sp.add_parser("down"); d.add_argument("--drop-db", action="store_true"); d.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "env-check": cmd_env_check, "up": cmd_up, "status": cmd_status, "verify": cmd_verify, "down": cmd_down}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
