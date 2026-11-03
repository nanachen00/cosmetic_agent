"""Shadow backend provenance, VM condition reproduced locally (tests/simprod.py): the team code hard-codes
/var/lib/jenkins/tomod/cosmetic-agent and here that literal names a simulated production tree.
  before r6 (plain copy)  the shadow process imports the production orchestrator and reads the production .env -> check FAILs
  r6 init-shadow          the literals point at the copy -> check passes, overlay active, nothing from production
No server, port or real production path is touched.  Run from shadow-sys1/:  python3 -W ignore tests/test_provenance.py"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="prov-"))
os.environ["SHADOWPATCH_STATE"] = str(TMP / "state")
PROD_FILE = TMP / "prod_roots.json"; os.environ["SHADOWPATCH_PROD_FILE"] = str(PROD_FILE)      # read when shadowpatch is imported
sys.path[:0] = [str(HERE), str(ROOT)]
import simprod  # noqa: E402
import shadowpatch as SP, shadow_stack as SS  # noqa: E402
SPEC = ROOT / "patches" / "sys1-dispatch-v1.json"


def sp(*a): return subprocess.run([sys.executable, str(ROOT / "shadowpatch.py"), *map(str, a)], capture_output=True, text=True)


def check_only(shadow, *trees):
    env = {k: v for k, v in os.environ.items() if k != simprod.SENTINEL}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")          # no __pycache__ written into the simulated production tree
    cmd = [sys.executable, str(ROOT / "shadow_entry.py"), "--shadow-root", str(shadow), "--check-only"]
    for t in trees: cmd += ["--prod-tree", str(t)]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", env=env, cwd=str(TMP))
    return r.returncode, json.loads(r.stdout), r.stderr


def same(a, b): return Path(a).resolve() == Path(b).resolve()


class Provenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prod = simprod.make(TMP / "prod")
        PROD_FILE.write_text(json.dumps({"prod_roots": [str(cls.prod)]}))
        cls.snapshot = {p: p.read_bytes() for p in cls.prod.rglob("*") if p.is_file()}
        cls.old = TMP / "shadow-r5" / "cosmetic-agent"; simprod.plain_copy(cls.prod, cls.old)
        cls.new = TMP / "shadow-r6" / "cosmetic-agent"
        r = sp("init-shadow", "--src", cls.prod, "--dest", cls.new); assert r.returncode == 0, r.stdout + r.stderr
        cls.init_out = r.stdout
        for d in (cls.old, cls.new):
            r = sp("apply", "--spec", SPEC, "--shadow-root", d); assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls): shutil.rmtree(TMP, ignore_errors=True)

    def test_1_init_shadow_rewrites_and_records(self):
        files = {f["path"]: f for f in json.loads((self.new / ".shadow-copy").read_text())["rewrite"]["files"]}
        self.assertEqual(set(files), {"api/api_server.py", "agents/root/orchestrator.py"})
        self.assertEqual((files["api/api_server.py"]["n"], files["agents/root/orchestrator.py"]["n"]), (2, 1))
        self.assertEqual(files["api/api_server.py"]["sha_before"], hashlib.sha256((self.prod / "api/api_server.py").read_bytes()).hexdigest())
        self.assertEqual(SP.residual_literals(self.new), [])
        text = (self.new / "api/api_server.py").read_text(encoding="utf-8")
        self.assertIn(f"sys.path.insert(0, '{self.new.resolve().as_posix()}')", text)
        self.assertIn(f"'{self.prod.as_posix()}-stable/x'", text, "another tree must not be rewritten")
        self.assertIn("api/api_server.py", self.init_out)
        self.assertFalse((self.new / ".env").exists())
        self.assertEqual({p: p.read_bytes() for p in self.prod.rglob("*") if p.is_file()}, self.snapshot, "production tree changed")

    def test_2_before_fix_the_check_fails(self):
        rc, rep, err = check_only(self.old, self.prod)
        self.assertEqual((rc, rep["verdict"]), (2, "FAIL"), err)
        self.assertTrue(same(rep["orchestrator"]["file"], self.prod / "agents/root/orchestrator.py"))
        self.assertFalse(rep["orchestrator"]["overlay_active"])
        for m in ("agents.root.orchestrator", "tools.agent.intent_parser"): self.assertIn(m, rep["prod_modules"])
        self.assertTrue(rep["prod_sys_path"]); self.assertIn("agents.root.orchestrator", rep["foreign_app"])
        self.assertTrue(any(same(p, self.prod / ".env") for p in rep["opened"]), rep["opened"])
        self.assertIn(simprod.SENTINEL, rep["env_added"])
        self.assertTrue(SS.check_provenance(rep, self.old))

    def test_3_after_fix_the_check_passes(self):
        rc, rep, err = check_only(self.new, self.prod)
        self.assertEqual(rc, 0, json.dumps(rep["problems"], ensure_ascii=False) + err)
        o = rep["orchestrator"]
        self.assertTrue(same(o["file"], self.new / "agents/root/orchestrator.py"))
        self.assertTrue(o["overlay_active"] and o["overlay_enabled"]); self.assertTrue(same(o["overlay_file"], self.new / "agents/root/sys1_dispatch.py"))
        self.assertEqual((rep["prod_modules"], rep["foreign_app"], rep["prod_sys_path"], rep["opened"]), ([], [], [], []))
        self.assertNotIn(simprod.SENTINEL, rep["env_added"])
        self.assertEqual(SS.check_provenance(rep, self.new), [])

    def test_4_shadow_stack_refuses_old_copies_and_bad_reports(self):
        with self.assertRaises(SystemExit) as cm: SS.rewrite_state(self.old)
        self.assertIn("init-shadow again", str(cm.exception))
        self.assertIn("2 files rewritten", SS.rewrite_state(self.new))
        lying = {"verdict": "ok", "problems": [], "orchestrator": {"file": str(self.prod / "agents/root/orchestrator.py")}}
        self.assertTrue(SS.check_provenance(lying, self.new), "the orchestrator path is checked outside the backend too")
        above = self.new.parent / ".env"; above.write_text("X=1\n")
        try: self.assertEqual([Path(p).resolve() for p in SS.env_files_above(self.new)], [above.resolve()])
        finally: above.unlink()

    def test_5_unsafe_dest_refused_before_copying(self):
        dest = TMP / "sh{x}" / "cosmetic-agent"
        r = sp("init-shadow", "--src", self.prod, "--dest", dest)
        self.assertNotEqual(r.returncode, 0); self.assertIn("string literal", r.stdout + r.stderr); self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
