"""Guards + functional tests for shadowpatch and the Kev dispatch overlay.  Uses only tests/fixture (a tiny stand-in tree) and
tests/mock_kev.py on 127.0.0.1:8239.  Never reads or writes production paths."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest, importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SHADOWPATCH_STATE"] = tempfile.mkdtemp(prefix="fpstate-")      # tests never touch the real .patch-state
SP = [sys.executable, str(ROOT / "shadowpatch.py")]
SPEC = str(ROOT / "patches" / "kev-dispatch-v1.json")
PORT = 8239

def sp(*a, env=None):
    e = dict(os.environ); e.update(env or {})
    return subprocess.run(SP + list(a), capture_output=True, text=True, env=e)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fpt-"))
        self.sh = self.tmp / "shadow" / "cosmetic-agent"
        r = sp("init-shadow", "--src", str(ROOT / "tests" / "fixture" / "cosmetic-agent"), "--dest", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.orch = self.sh / "agents/root/orchestrator.py"; self.orig = self.orch.read_bytes()
    def tearDown(self):
        sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force"); shutil.rmtree(self.tmp, ignore_errors=True)

class Guards(Base):
    def test_apply_revert_identical(self):
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(b"SHADOW-KEV-PATCH", self.orch.read_bytes()); self.assertTrue((self.sh / "agents/root/kev_dispatch.py").exists())
        self.assertEqual(sp("verify", "--spec", SPEC, "--shadow-root", str(self.sh)).returncode, 0)
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orch.read_bytes(), self.orig); self.assertFalse((self.sh / "agents/root/kev_dispatch.py").exists())
        self.assertFalse((self.sh / ".kev-shadow-overlay").exists())
        self.assertEqual([p.name for p in self.orch.parent.iterdir() if p.name.endswith((".bak", ".orig", ".patch"))], [])
    def test_double_apply_refused(self):
        sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh))
        self.assertNotEqual(r.returncode, 0); self.assertIn("already applied", r.stderr + r.stdout)
    def test_refuse_production_root(self):
        fake = self.tmp / "prod" / "cosmetic-agent"; shutil.copytree(self.sh, fake)
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(fake), env={"SHADOWPATCH_PROD_ROOTS": str(fake)})
        self.assertNotEqual(r.returncode, 0); self.assertIn("G1", r.stderr + r.stdout); self.assertEqual((fake / "agents/root/orchestrator.py").read_bytes(), self.orig)
    def test_refuse_inside_production_root(self):
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh), env={"SHADOWPATCH_PROD_ROOTS": str(self.tmp)})
        self.assertNotEqual(r.returncode, 0); self.assertIn("G1", r.stderr + r.stdout)
    def test_refuse_unmarked_dir(self):
        d = self.tmp / "plain"; shutil.copytree(self.sh, d); (d / ".shadow-copy").unlink()
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(d)); self.assertNotEqual(r.returncode, 0); self.assertIn("G2", r.stderr + r.stdout)
    def test_refuse_symlink_target(self):
        real = self.tmp / "elsewhere.py"; real.write_bytes(self.orig); self.orch.unlink(); self.orch.symlink_to(real)
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertNotEqual(r.returncode, 0); self.assertIn("G3", r.stderr + r.stdout)
        self.assertEqual(real.read_bytes(), self.orig)
    def test_refuse_hardlink_target(self):
        alias = self.tmp / "alias.py"; os.link(self.orch, alias)
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertNotEqual(r.returncode, 0); self.assertIn("hard links", r.stderr + r.stdout)
        self.assertEqual(alias.read_bytes(), self.orig)
    def test_anchor_drift_refused_and_untouched(self):
        self.orch.write_bytes(self.orig.replace(b"elif len(user_input) > 20:", b"elif len(user_input) > 21:"))
        before = self.orch.read_bytes(); r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh))
        self.assertNotEqual(r.returncode, 0); self.assertIn("G5", r.stderr + r.stdout); self.assertEqual(self.orch.read_bytes(), before)
        self.assertFalse((self.sh / "agents/root/kev_dispatch.py").exists())
    def test_revert_detects_foreign_edit(self):
        sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.orch.write_bytes(self.orch.read_bytes() + b"\n# foreign edit\n")
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertNotEqual(r.returncode, 0)
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force"); self.assertEqual(r.returncode, 0); self.assertEqual(self.orch.read_bytes(), self.orig)
    def test_revert_works_after_marker_lost(self):
        sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); (self.sh / ".shadow-copy").unlink()
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr + r.stdout); self.assertEqual(self.orch.read_bytes(), self.orig)
    def test_anchors_readonly(self):
        before = self.orch.read_bytes(); r = sp("anchors", "--spec", SPEC, "--src", str(self.sh)); self.assertEqual(r.returncode, 0, r.stdout); self.assertEqual(self.orch.read_bytes(), before)
    def test_revert_all(self):
        sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); r = sp("revert-all"); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.orch.read_bytes(), self.orig)

class Functional(Base):
    """Run the patched fixture orchestrator against the mock Kev: dispatch table, no-LLM guarantee, error handling, arm switch."""
    @classmethod
    def setUpClass(cls):
        try: import pydantic  # noqa
        except ImportError: raise unittest.SkipTest("pydantic not installed")
        cls.kev = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_kev.py"), "--port", str(PORT)])
        for _ in range(50):
            try:
                import urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/models", timeout=0.5); break
            except Exception: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls):
        cls.kev.terminate(); cls.kev.wait()
    def load(self, arm, extra=None):
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.ctl = self.tmp / "ctl.json"; self.trace = self.tmp / "trace.jsonl"
        json.dump(dict({"arm": arm, "trace": str(self.trace), "kev_url": f"http://127.0.0.1:{PORT}"}, **(extra or {})), open(self.ctl, "w"))
        os.environ["KEV_DISPATCH_CONTROL"] = str(self.ctl)
        for m in [m for m in sys.modules if m.split(".")[0] in ("agents", "tools", "schemas")]: del sys.modules[m]
        sys.path.insert(0, str(self.sh)); self.addCleanup(lambda: sys.path.remove(str(self.sh)))
        mod = importlib.import_module("agents.root.orchestrator"); ip = importlib.import_module("tools.agent.intent_parser"); ip.CALLS.clear()
        return mod.Orchestrator(), ip
    def test_dispatch_table_no_llm(self):
        o, ip = self.load("kev_route")
        cases = {"請推薦預算1000元以下的乳液，我是乾性肌": ("dispatch", "product_agent:dry:乳液:1000"),
                 "玻尿酸和神經醯胺有什麼差別？": ("dispatch", "knowledge_agent"),
                 "我用了水楊酸後臉很刺痛又紅": ("dispatch", "safety_agent"),
                 "幫我記錄今天兩頰偏乾": ("skin_tracking", None), "謝謝你，我先考慮一下。": ("clarify:routing", None)}
        for q, (stage, agent) in cases.items():
            r = o._handle_new_input(q); self.assertEqual(r["stage"], stage, (q, r))
            if agent: self.assertEqual(r["agent"], agent, (q, r))
        self.assertEqual(ip.CALLS, [], "LLM parse_intent was called under a Kev arm")
        rows = [json.loads(l) for l in self.trace.read_text().splitlines()]; self.assertEqual(len(rows), len(cases)); self.assertTrue(all("kev_ms" in r for r in rows))
    def test_kev_down_means_clarify_not_llm(self):
        o, ip = self.load("kev_route", {"kev_url": "http://127.0.0.1:8238", "timeout_s": 1})
        r = o._handle_new_input("請推薦乳液"); self.assertEqual(r["stage"], "clarify:routing"); self.assertEqual(ip.CALLS, [])
        self.assertIn("kev_error", json.loads(self.trace.read_text().splitlines()[-1])["why"])
    def test_arm_off_is_passthrough_and_switchable(self):
        o, ip = self.load("off")
        o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)
        json.dump({"arm": "kev_route", "kev_url": f"http://127.0.0.1:{PORT}"}, open(self.ctl, "w")); os.utime(self.ctl, (time.time() + 5, time.time() + 5))
        o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1, "arm switch not picked up / LLM still used")
    def test_inert_without_marker(self):
        o, ip = self.load("kev_route"); (self.sh / ".kev-shadow-overlay").unlink()
        o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1, "overlay must be inert without marker")
    def test_kev_slots_arm(self):
        o, ip = self.load("kev_slots"); r = o._handle_new_input("我是乾性肌，想找保濕的乳液，預算800元以下")
        self.assertEqual(r["agent"], "product_agent:dry:乳液:800"); self.assertEqual(ip.CALLS, [])
        self.assertEqual(json.loads(self.trace.read_text().splitlines()[-1])["slots_kev"].get("concern"), "保濕")

if __name__ == "__main__":
    unittest.main(verbosity=2)
