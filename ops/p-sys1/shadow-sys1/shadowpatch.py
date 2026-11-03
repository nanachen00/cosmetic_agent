#!/usr/bin/env python3
"""shadowpatch (shadow-sys1 copy) — apply / revert a patch on a SHADOW copy only.  Never touches production source.
Copied from fullpath-kev/shadowpatch.py; changes: production roots also come from prod_roots.json (current paths, confirmed by the
operator), overlay marker/control/tag come from the spec, add_files may create sub-directories (removed again on revert), and a
tree that already carries ANY shadow patch tag (Kev or Sys1) is refused.

Hard guards (every command, before any write):
  G1  shadow root must resolve (realpath) outside every production root and must not contain one.
  G2  shadow root must be positively identified: marker file `.shadow-copy` in it, OR it lies under an
      allow-listed parent (this suite's `shadow/`, the benchmark `results/` / `.state/` trees, or
      $SHADOWPATCH_ALLOW_PARENT, colon-separated).
  G3  every file written must be a regular file, not a symlink, nlink == 1 (no hard link into production),
      and must not share (st_dev, st_ino) with the production file of the same relative path.
  G4  production file sha256 (if present/readable) is recorded before and compared after; any change aborts.
  G5  hunks are anchored on exact text that must occur exactly once; whole patched file must compile.
Original bytes are kept OUTSIDE the source tree (.patch-state/), never as .bak/.orig next to the source.

usage:
  shadowpatch.py apply   --spec patches/X.json --shadow-root DIR
  shadowpatch.py revert  --spec patches/X.json --shadow-root DIR [--force]
  shadowpatch.py verify  --spec patches/X.json --shadow-root DIR
  shadowpatch.py status  [--shadow-root DIR]
  shadowpatch.py revert-all
  shadowpatch.py anchors --spec X --src DIR           (read-only anchor check, DIR may be production)
  shadowpatch.py init-shadow --src DIR --dest DIR     (read-only copy of a tree into a marked shadow dir; skips secrets;
                                                       points hard-coded production paths in the copied .py files at the copy)
"""
from __future__ import annotations
import argparse, hashlib, json, os, py_compile, re, shutil, stat, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("SHADOWPATCH_STATE", str(HERE / ".patch-state")))
MARKER = ".shadow-copy"
PATCH_TAG = "SHADOW-SYS1-PATCH"
KNOWN_TAGS = ("SHADOW-KEV-PATCH", "SHADOW-SYS1-PATCH")
PROD_FILE = Path(os.environ.get("SHADOWPATCH_PROD_FILE", str(HERE / "prod_roots.json")))
DEFAULT_PROD = ["/var/lib/jenkins/tomod/cosmetic-agent", "/var/lib/jenkins/tomod/cosmetic-agent-stable",
                "/var/lib/jenkins/tomod/backend", "/var/lib/jenkins/tomod/core"]
DEFAULT_ALLOW = [str(HERE / "shadow"),
                 "/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark/results",
                 "/var/lib/jenkins/tomod/ops/blink-root-intent-system-one-benchmark/.state",
                 "/var/lib/jenkins/tomod/ops/local-llm-path-benchmark/results",
                 "/var/lib/jenkins/tomod/ops/local-llm-path-benchmark/.state"]
SECRET_NAMES = {".env", ".git", "secrets", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
SECRET_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".crt")
# The team code hard-codes this tree: sys.path.insert(0, …) in api_server/orchestrator/product_agent/copywriting_tool/graph_search,
# load_dotenv('…/.env') in daily_content, env_file='…/.env' for skin_tracking.  Left as is, a shadow copy imports production modules.
PROD_SRC_LITERAL = "/var/lib/jenkins/tomod/cosmetic-agent"
UNSAFE_IN_LITERAL = re.compile(r"['\"\\{}\x00-\x1f]")  # the replacement goes inside Python string literals (incl. f-strings)


class Refuse(SystemExit):
    def __init__(self, msg): super().__init__(f"[REFUSED] {msg}")


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def prod_roots_from_file() -> list[str]:
    try: return [str(x) for x in json.loads(PROD_FILE.read_text(encoding="utf-8")).get("prod_roots", [])]
    except FileNotFoundError: return []
    except Exception as e: raise Refuse(f"{PROD_FILE} unreadable: {e}")


def prod_roots() -> list[Path]:
    roots = list(DEFAULT_PROD) + prod_roots_from_file()
    roots += [x for x in os.environ.get("SHADOWPATCH_PROD_ROOTS", "").split(os.pathsep) if x]   # ":" on Linux; ";" keeps D:\ intact
    return [Path(r).resolve() for r in roots]       # resolve() works for non-existing paths too


def allow_parents() -> list[Path]:
    a = list(DEFAULT_ALLOW) + [x for x in os.environ.get("SHADOWPATCH_ALLOW_PARENT", "").split(":") if x]
    return [Path(x).resolve() for x in a]


def _within(p: Path, parent: Path) -> bool:
    try: p.relative_to(parent); return True
    except ValueError: return False


def check_shadow_root(root: str | Path, extra_ok: list[str] | None = None, applied_before: bool = False) -> Path:
    raw = Path(root)
    if not raw.exists() or not raw.is_dir(): raise Refuse(f"shadow root is not a directory: {raw}")
    real = raw.resolve()
    for pr in prod_roots():
        if real == pr or _within(real, pr): raise Refuse(f"G1 shadow root is inside production root {pr}")
        if _within(pr, real): raise Refuse(f"G1 shadow root contains production root {pr}")
    if not (applied_before or (real / MARKER).is_file() or any(_within(real, a) for a in allow_parents())
            or any(real == Path(x).resolve() for x in (extra_ok or []))):
        raise Refuse(f"G2 {real} has no `{MARKER}` marker and is not under an allow-listed shadow parent "
                     f"(create a marked copy with `init-shadow`, or set SHADOWPATCH_ALLOW_PARENT)")
    return real


def find_target(root: Path, rel: str) -> Path:
    cands = [root / rel] + [p for p in root.glob(f"*/{rel}")]
    cands = [c for c in cands if c.is_file()]
    if len(cands) != 1: raise Refuse(f"target {rel!r} must exist exactly once under {root} (found {len(cands)})")
    return cands[0]


def guard_file(p: Path, root: Path):
    if p.is_symlink(): raise Refuse(f"G3 {p} is a symlink")
    real = p.resolve()
    if not _within(real, root): raise Refuse(f"G3 {p} resolves outside the shadow root")
    st = p.stat()
    if not stat.S_ISREG(st.st_mode): raise Refuse(f"G3 {p} is not a regular file")
    if st.st_nlink != 1: raise Refuse(f"G3 {p} has {st.st_nlink} hard links (could alias a production file)")
    rel = real.relative_to(root)
    for pr in prod_roots():
        for k in range(0, min(3, len(rel.parts))):          # shadow root may be the tree itself or its parent
            cand = pr / Path(*rel.parts[k:])
            try:
                q = cand.stat()
                if (q.st_dev, q.st_ino) == (st.st_dev, st.st_ino): raise Refuse(f"G3 {p} is the same inode as production {cand}")
            except FileNotFoundError: pass


def prod_fingerprint(rel_targets: list[str]) -> dict:
    fp = {}
    for pr in prod_roots():
        for rel in rel_targets:
            for cand in (pr / rel,):
                if cand.is_file():
                    try: fp[str(cand)] = sha(cand)
                    except OSError: fp[str(cand)] = "UNREADABLE"
    return fp


def state_dir(root: Path, spec_id: str) -> Path:
    return STATE / hashlib.sha256(str(root).encode()).hexdigest()[:16] / spec_id


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("id", "target", "hunks"): assert k in spec, f"spec missing {k}"
    return spec


def atomic_write(p: Path, data: bytes):
    mode = p.stat().st_mode & 0o777 if p.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".shadowpatch-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, mode | 0o200)
        os.replace(tmp, p)
    except BaseException:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise


def compile_check(text: str, name: str):
    try: compile(text, name, "exec")
    except SyntaxError as e: raise Refuse(f"G5 patched {name} does not compile: {e}")


def cmd_apply(a):
    spec = load_spec(a.spec); root = check_shadow_root(a.shadow_root, spec.get("allow_roots"))
    target = find_target(root, spec["target"]); guard_file(target, root)
    sd = state_dir(root, spec["id"]); manifest = sd / "manifest.json"
    if manifest.exists(): raise Refuse(f"already applied ({manifest}); run revert first")
    orig = target.read_bytes(); text = orig.decode("utf-8")
    tag = spec.get("tag", PATCH_TAG)
    if any(t in text for t in KNOWN_TAGS): raise Refuse(f"{target} already carries a shadow patch tag {KNOWN_TAGS} (applied by something else?) — start from a clean shadow copy")
    prod_before = prod_fingerprint([spec["target"]])
    new = text
    for h in spec["hunks"]:
        n = new.count(h["old"])
        if n != 1: raise Refuse(f"G5 hunk {h['id']}: anchor occurs {n} times (need exactly 1) — production source drifted, update the spec")
        new = new.replace(h["old"], h["new"], 1)
    compile_check(new, str(target))
    pkg_root = target.parents[len(Path(spec["target"]).parts) - 1]     # dir that contains agents/ (e.g. .../cosmetic-agent)
    added = [(HERE / ad["src"], pkg_root / ad["dest"]) for ad in spec.get("add_files", [])]
    for src, dest in added:
        if not src.is_file(): raise Refuse(f"payload missing: {src}")
        if dest.exists() or dest.is_symlink(): raise Refuse(f"{dest} already exists")
        if not _within(dest.resolve(), root): raise Refuse(f"G3 {dest} outside shadow root")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "orig").write_bytes(orig); os.sync() if hasattr(os, "sync") else None
    m = {"spec_id": spec["id"], "shadow_root": str(root), "target": str(target), "orig_sha": hashlib.sha256(orig).hexdigest(),
         "patched_sha": hashlib.sha256(new.encode()).hexdigest(), "added": [str(d) for _, d in added],
         "added_sha": {}, "added_dirs": [], "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "prod_fingerprint": prod_before}
    try:
        atomic_write(target, new.encode())
        for src, dest in added:
            missing = []
            q = dest.parent
            while not q.exists(): missing.append(q); q = q.parent
            for q in reversed(missing): q.mkdir(); m["added_dirs"].append(str(q))
            dest.write_bytes(src.read_bytes()); m["added_sha"][str(dest)] = sha(dest)
        if spec.get("overlay_marker"):
            mk = pkg_root / spec["overlay_marker"]
            mk.write_text(json.dumps({"shadow_root": str(root), "pkg_root": str(pkg_root), "spec_id": spec["id"],
                                      "prod_roots": [str(p) for p in prod_roots()],
                                      "control": os.environ.get("SYS1_OVERLAY_CONTROL", str(HERE / spec.get("control_default", "control/sys1_dispatch.json")))}) + "\n")
            m["added"].append(str(mk))
        (sd / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    except BaseException:
        atomic_write(target, orig)                      # roll back immediately
        for _, d in added:
            try: d.unlink()
            except FileNotFoundError: pass
        for q in reversed(m["added_dirs"]):
            try: Path(q).rmdir()
            except OSError: pass
        shutil.rmtree(sd, ignore_errors=True); raise
    if prod_fingerprint([spec["target"]]) != prod_before: raise Refuse("G4 production file changed while patching (not by us) — investigate")
    print(f"APPLIED {spec['id']} -> {target}\n  orig_sha   {m['orig_sha']}\n  patched_sha {m['patched_sha']}\n  state      {sd}")


def cmd_revert(a, quiet=False):
    spec = load_spec(a.spec); root = check_shadow_root(a.shadow_root, spec.get("allow_roots"), applied_before=True)
    sd = state_dir(root, spec["id"]); mf = sd / "manifest.json"
    if not mf.exists():
        if not quiet: print("NOTHING TO REVERT (not applied)")
        return 0
    m = json.loads(mf.read_text()); target = Path(m["target"]); guard_file(target, root)
    cur = sha(target)
    if cur != m["patched_sha"] and not a.force:
        raise Refuse(f"{target} differs from what we wrote (sha {cur[:12]}…); someone edited it. Use --force to restore the original anyway")
    orig = (sd / "orig").read_bytes()
    if hashlib.sha256(orig).hexdigest() != m["orig_sha"]: raise Refuse("stored original is corrupt (sha mismatch)")
    atomic_write(target, orig)
    for f in m["added"]:
        try: Path(f).unlink()
        except FileNotFoundError: pass
    for q in reversed(m.get("added_dirs", [])):
        try: Path(q).rmdir()
        except OSError: pass
    if sha(target) != m["orig_sha"]: raise Refuse("post-revert hash mismatch")
    done = sd.with_name(sd.name + f".reverted-{time.time_ns()}")      # ns + pid: two reverts in the same second must not collide
    while done.exists(): done = done.with_name(done.name + f"-{os.getpid()}")
    sd.rename(done)
    print(f"REVERTED {spec['id']} -> {target}  sha={m['orig_sha'][:12]}… (record: {done.name})")
    return 0


def cmd_verify(a):
    spec = load_spec(a.spec); root = check_shadow_root(a.shadow_root, spec.get("allow_roots"), applied_before=True)
    sd = state_dir(root, spec["id"]); mf = sd / "manifest.json"
    if not mf.exists(): print("NOT APPLIED"); return 1
    m = json.loads(mf.read_text()); target = Path(m["target"])
    ok = sha(target) == m["patched_sha"] and all(Path(f).is_file() and sha(Path(f)) == h for f, h in m["added_sha"].items())
    prod_now = prod_fingerprint([spec["target"]]); prod_ok = prod_now == m["prod_fingerprint"]
    print(json.dumps({"applied": True, "intact": ok, "production_unchanged": prod_ok, "target": str(target)}, indent=2))
    return 0 if ok and prod_ok else 2


def cmd_status(a):
    rows = []
    for mf in sorted(STATE.glob("*/*/manifest.json")) if STATE.exists() else []:
        m = json.loads(mf.read_text())
        if a.shadow_root and Path(m["shadow_root"]) != Path(a.shadow_root).resolve(): continue
        t = Path(m["target"]); rows.append({"spec": m["spec_id"], "shadow_root": m["shadow_root"], "applied_at": m["applied_at"],
                                            "target_exists": t.exists(), "intact": t.exists() and sha(t) == m["patched_sha"]})
    print(json.dumps(rows, indent=2, ensure_ascii=False)); return 0


def cmd_revert_all(a):
    n = 0; bad = 0
    for mf in sorted(STATE.glob("*/*/manifest.json")) if STATE.exists() else []:
        m = json.loads(mf.read_text())
        if not Path(m["shadow_root"]).is_dir():
            mf.parent.rename(mf.parent.with_name(mf.parent.name + f".stale-{int(time.time())}"))
            print(f"PRUNED {m['spec_id']}: shadow root gone ({m['shadow_root']})"); continue
        spec_path = HERE / "patches" / f"{m['spec_id']}.json"
        ns = argparse.Namespace(spec=str(spec_path), shadow_root=m["shadow_root"], force=a.force)
        try: cmd_revert(ns, quiet=True); n += 1
        except SystemExit as e: bad += 1; print(e)
    print(f"revert-all: reverted {n}, failed {bad}"); return 0 if not bad else 2


def cmd_anchors(a):
    """READ-ONLY: do all hunk anchors still occur exactly once in <src>/<target>?  (src may be production — it is only read)"""
    spec = load_spec(a.spec); src = Path(a.src)
    cands = [src / spec["target"]] + list(src.glob(f"*/{spec['target']}")); cands = [c for c in cands if c.is_file()]
    if len(cands) != 1: print(f"target not found exactly once under {src}"); return 2
    text = cands[0].read_text(encoding="utf-8"); bad = 0
    for h in spec["hunks"]:
        n = text.count(h["old"]); bad += n != 1; print(f"  {h['id']:<28} anchors={n} {'OK' if n == 1 else 'MISMATCH'}")
    print(f"{cands[0]}  sha256={sha(cands[0])[:16]}…  -> {'all anchors match' if not bad else str(bad) + ' anchor(s) drifted: update patches/*.json'}")
    return 0 if not bad else 3


def literal_pattern(lits) -> re.Pattern:
    """Exact path literals not followed by another name character, so …/cosmetic-agent-stable is left alone."""
    return re.compile(rb"(?:" + rb"|".join(re.escape(x.encode()) for x in sorted(set(lits), key=len, reverse=True)) + rb")(?![\w.-])")


def rewrite_prod_literals(d: Path, src: Path, lits: list[str]) -> list[dict]:
    """Point every hard-coded source-tree path in the copied .py files at the copy.  Writes only regular files under d (G3);
    a file that compiled before must still compile (G5); its source counterpart must not change meanwhile (G4)."""
    to = d.as_posix()
    if UNSAFE_IN_LITERAL.search(to): raise Refuse(f"shadow path {to!r} cannot be written into a Python string literal")
    pat = literal_pattern(lits); done = []
    for p in sorted(d.rglob("*.py")):
        if p.is_symlink() or not p.is_file(): continue
        data = p.read_bytes(); new, n = pat.subn(to.encode(), data)
        if not n: continue
        guard_file(p, d)
        rel = p.relative_to(d).as_posix(); counterpart = src / rel
        src_before = sha(counterpart) if counterpart.is_file() else None
        try: compile(data, str(p), "exec"); compiled = True
        except (SyntaxError, ValueError): compiled = False       # already broken upstream: rewrite anyway, nothing to keep
        if compiled:
            try: compile(new, str(p), "exec")
            except (SyntaxError, ValueError) as e: raise Refuse(f"G5 rewritten {rel} does not compile: {e}")
        atomic_write(p, new)
        if src_before and sha(counterpart) != src_before: raise Refuse(f"G4 source {counterpart} changed during init-shadow — investigate")
        done.append({"path": rel, "n": n, "sha_before": hashlib.sha256(data).hexdigest(), "sha_after": hashlib.sha256(new).hexdigest()})
    return done


def residual_literals(root: Path) -> list[str]:
    """.py files in a shadow copy that still name the source tree (copy made before the rewrite existed, or files added later).
    Overlay payload files (add_files of patches/*.json) are ours and list the production roots on purpose (their own guard)."""
    try: lits = json.loads((root / MARKER).read_text(encoding="utf-8"))["rewrite"]["literals"]
    except Exception: lits = [PROD_SRC_LITERAL]
    ours = {ad["dest"] for f in (HERE / "patches").glob("*.json") for ad in json.loads(f.read_text(encoding="utf-8")).get("add_files", [])}
    pat = literal_pattern(lits)
    return [rel for p in sorted(root.rglob("*.py")) if p.is_file() and (rel := p.relative_to(root).as_posix()) not in ours and pat.search(p.read_bytes())]


def cmd_init_shadow(a):
    src = Path(a.src).resolve(); dest = Path(a.dest)
    if dest.exists() and any(dest.iterdir()): raise Refuse(f"dest {dest} not empty")
    d = dest.resolve()
    for pr in prod_roots():
        if d == pr or _within(d, pr): raise Refuse(f"dest {d} is inside production root {pr}")
    if UNSAFE_IN_LITERAL.search(d.as_posix()): raise Refuse(f"dest {d.as_posix()!r} cannot be written into a Python string literal (quotes, backslash, braces)")
    copied = skipped = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(part in SECRET_NAMES for part in rel.parts) or p.name.startswith(".env") or p.suffix in SECRET_SUFFIX or p.is_symlink():
            skipped += 1; continue
        t = d / rel
        if p.is_dir(): t.mkdir(parents=True, exist_ok=True)
        elif p.is_file(): t.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, t); copied += 1   # copyfile: new inode, no hard link
    lits = sorted({PROD_SRC_LITERAL, src.as_posix(), Path(os.path.abspath(a.src)).as_posix()})
    rewritten = rewrite_prod_literals(d, src, lits)
    (d / MARKER).write_text(json.dumps({"from": str(src), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                        "rewrite": {"literals": lits, "to": d.as_posix(), "files": rewritten}}, indent=1) + "\n")
    print(f"init-shadow: {copied} files copied, {skipped} skipped (secrets/links/caches) -> {d}")
    print(f"  {len(rewritten)} .py files: {' / '.join(lits)} -> {d.as_posix()} (recorded in {MARKER})")
    for r in rewritten: print(f"    {r['path']}  x{r['n']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("apply", "revert", "verify"):
        s = sp.add_parser(n); s.add_argument("--spec", required=True); s.add_argument("--shadow-root", required=True)
        if n == "revert": s.add_argument("--force", action="store_true")
    s = sp.add_parser("status"); s.add_argument("--shadow-root")
    s = sp.add_parser("revert-all"); s.add_argument("--force", action="store_true")
    s = sp.add_parser("anchors"); s.add_argument("--spec", required=True); s.add_argument("--src", required=True)
    s = sp.add_parser("init-shadow"); s.add_argument("--src", required=True); s.add_argument("--dest", required=True)
    a = ap.parse_args()
    return {"apply": cmd_apply, "revert": cmd_revert, "verify": cmd_verify, "status": cmd_status,
            "revert-all": cmd_revert_all, "anchors": cmd_anchors, "init-shadow": cmd_init_shadow}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
