"""T7 (S3 shadow compare) + T8 (switch / rollback) tests on mocks only.
mock Sys1 = gateB-colab mock_kev on 127.0.0.1:8235 · mock backend (real overlay on a patched fixture shadow copy) on :8212.
Run from shadow-sys1/:  python3 -W ignore tests/test_s3_t8.py"""
import csv, hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]; GB_DIR = ROOT.parent / "gateB-colab"; PY = sys.executable
os.environ["SHADOWPATCH_STATE"] = tempfile.mkdtemp(prefix="s3state-"); os.environ["SHADOWPATCH_PROD_FILE"] = str(Path(tempfile.mkdtemp()) / "none.json")
SYS1_PORT, BACKEND = 8235, 8212


def run(*a, env=None, timeout=300):
    e = dict(os.environ); e.update(env or {}); return subprocess.run([PY] + [str(x) for x in a], capture_output=True, text=True, env=e, cwd=str(ROOT), timeout=timeout)


def wait(url, n=100):
    for _ in range(n):
        try: urllib.request.urlopen(url, timeout=0.5); return True
        except Exception: time.sleep(0.1)
    return False


_GUESTS = {}


def say(text, conv="c1"):
    """Same flow as s3_shadow.drive: one guest session per conv, then the production ChatRequest body."""
    import http.cookiejar
    if conv not in _GUESTS:
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with op.open(urllib.request.Request(f"http://127.0.0.1:{BACKEND}/api/auth/guest-session", data=b"{}", headers={"Content-Type": "application/json"}), timeout=10) as r:
            _GUESTS[conv] = (op, json.loads(r.read())["user_id"])
    op, uid = _GUESTS[conv]
    body = json.dumps({"user_id": uid, "conversation_id": conv, "message": text, "flow": "auto"}, ensure_ascii=False).encode()
    with op.open(urllib.request.Request(f"http://127.0.0.1:{BACKEND}/api/chat/message", data=body, headers={"Content-Type": "application/json"}), timeout=30) as r: return json.loads(r.read())


class Stack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: import pydantic  # noqa
        except ImportError: raise unittest.SkipTest("pydantic not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="s3t-")); cls.sh = cls.tmp / "shadow" / "cosmetic-agent"
        assert run(ROOT / "shadowpatch.py", "init-shadow", "--src", ROOT / "tests/fixture/cosmetic-agent", "--dest", cls.sh).returncode == 0
        r = run(ROOT / "shadowpatch.py", "apply", "--spec", ROOT / "patches/sys1-dispatch-v1.json", "--shadow-root", cls.sh); assert r.returncode == 0, r.stderr
        th = json.load(open(GB_DIR / "holdout/frozen_thresholds.json", encoding="utf-8"))["by_kev"]["0.8b"]["v2"]
        fz = cls.tmp / "frozen.json"; fz.write_text(json.dumps({"cand": "mock", "thresholds": th, "sha256": hashlib.sha256(json.dumps(th, sort_keys=True).encode()).hexdigest()}))
        cls.trace = cls.tmp / "trace.jsonl"; cls.ctl = cls.tmp / "ctl.json"
        cls.ctl.write_text(json.dumps({"arm": "off", "canary_pct": 0, "sys1_url": f"http://127.0.0.1:{SYS1_PORT}", "model": "mock", "frozen": str(fz), "timeout_s": 1,
                                       "trace": str(cls.trace), "trace_text": False, "error_fallback": "safety_or_clarify"}))
        os.environ["SYS1_DISPATCH_CONTROL"] = str(cls.ctl); os.environ["SYS1CTL_LOG"] = str(cls.tmp / "switch_log.jsonl")
        cls.start_sys1()
        cls.be = subprocess.Popen([PY, str(ROOT / "tests/mock_backend_sys1.py"), "--port", str(BACKEND), "--shadow-root", str(cls.sh)], stderr=subprocess.DEVNULL)
        assert wait(f"http://127.0.0.1:{BACKEND}/")

    @classmethod
    def start_sys1(cls):
        cls.s1 = subprocess.Popen([PY, str(GB_DIR / "vendor_fk/tests/mock_kev.py"), "--port", str(SYS1_PORT)]); assert wait(f"http://127.0.0.1:{SYS1_PORT}/v1/models")

    @classmethod
    def tearDownClass(cls):
        for p in (cls.be, cls.s1): p.terminate(); p.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ctl_now(self): return json.loads(self.ctl.read_text())

    def ctl_cmd(self, *a): return run(ROOT / "sys1ctl.py", "--control", self.ctl, *a)

    # ------------------------------------------------------------------ T7
    def test_1_s3_drive_and_analyze(self):
        start = time.time()
        r = run(ROOT / "s3_shadow.py", "drive", "--base-url", f"http://127.0.0.1:{BACKEND}", "--scenarios", ROOT / "tests/fixture_scenarios.json", "--control", self.ctl, "--trace-text", "--settle", "1.5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertEqual(self.ctl_now()["arm"], "off", "control must be restored")
        out = self.tmp / "s3"; r = run(ROOT / "s3_shadow.py", "analyze", "--trace", self.trace, "--source", "synthetic", "--out", out, "--since", start)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = json.loads((out / "s3_metrics.json").read_text()); self.assertEqual(m["n"], 10); self.assertEqual(m["sys1_error_rate"], 0.0)   # 10 turns in fixture_scenarios.json
        self.assertIn("限制", (out / "SUMMARY.md").read_text()); self.assertIsNotNone(m["latency_ms"]["sys1_http"]["p95"])
        dis = list(csv.reader(open(out / "disagreements.csv", encoding="utf-8")))[1:]; self.assertTrue(any("孕婦" in row[2] or "腫" in row[2] for row in dis) or m["agreement"] == 1.0)
        sl = list(csv.reader(open(out / "safety_llm_only.csv", encoding="utf-8")))[1:]
        self.assertEqual(len(sl), m["llm_safety_sys1_not"])
        self.assertNotEqual(run(ROOT / "s3_shadow.py", "analyze", "--trace", self.trace, "--source", "consented", "--out", out).returncode, 0, "consented needs a note")

    def test_2b_chat_requires_guest_session(self):
        req = urllib.request.Request(f"http://127.0.0.1:{BACKEND}/api/chat/message", json.dumps({"user_id": "x", "message": "hi"}).encode(), {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm: urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 401)

    def test_2_s3_guards(self):
        r = run(ROOT / "s3_shadow.py", "drive", "--base-url", "http://127.0.0.1:8010", "--scenarios", ROOT / "tests/fixture_scenarios.json", "--control", self.ctl)
        self.assertNotEqual(r.returncode, 0); self.assertIn("REFUSED", r.stdout + r.stderr)

    # ------------------------------------------------------------------ T8
    def test_3_switch_rules(self):
        self.assertNotEqual(self.ctl_cmd("canary", "5").returncode, 0, "canary without --approved-by must fail")
        r = self.ctl_cmd("canary", "25", "--approved-by", "t"); self.assertNotEqual(r.returncode, 0); self.assertIn("不能跳級", r.stdout)
        for p in ("5", "25", "100"): r = self.ctl_cmd("canary", p, "--approved-by", "t"); self.assertEqual(r.returncode, 0, r.stdout); self.assertEqual(self.ctl_now()["canary_pct"], int(p))
        r = self.ctl_cmd("rollback", "--reason", "test"); self.assertEqual(r.returncode, 0); self.assertEqual((self.ctl_now()["arm"], self.ctl_now()["canary_pct"]), ("off", 0))
        good = self.ctl_now(); self.ctl.write_text(json.dumps(dict(good, frozen="/nonexistent")))
        r = self.ctl_cmd("canary", "5", "--approved-by", "t"); self.assertNotEqual(r.returncode, 0); self.assertIn("frozen", r.stdout); self.ctl.write_text(json.dumps(good))
        acts = [json.loads(l)["action"] for l in open(self.tmp / "switch_log.jsonl")]; self.assertEqual(acts[-1], "rollback")

    def test_4_rollback_drill_sys1_dies(self):
        """canary 100% → Sys1 掛掉 → 回滾檢查觸發並自動設回 off → 之後的請求走舊 LLM。"""
        for p in ("5", "25", "100"): self.assertEqual(self.ctl_cmd("canary", p, "--approved-by", "drill").returncode, 0)
        for i in range(22): self.assertEqual(say("請推薦乳液", f"d{i}")["llm_calls"], 0)
        chk = lambda *x: run(ROOT / "rollback_check.py", "--control", self.ctl, "--window-s", "120", *x)
        self.assertEqual(chk().returncode, 0, chk().stdout)
        self.s1.terminate(); self.s1.wait()
        try:
            for i in range(22): r = say("請推薦乳液", f"e{i}"); self.assertEqual(r["stage"], "clarify:routing"); self.assertEqual(r["llm_calls"], 0)
            r = chk("--apply"); self.assertEqual(r.returncode, 10, r.stdout); self.assertIn("R1", r.stdout)
            self.assertEqual(self.ctl_now()["arm"], "off"); self.assertEqual(say("請推薦乳液", "after")["llm_calls"], 1, "after rollback the old LLM path must be used")
            last = [json.loads(l) for l in open(self.tmp / "switch_log.jsonl")][-1]; self.assertEqual(last["action"], "auto_rollback")
        finally:
            type(self).start_sys1()

    def test_5_rules_p95_5xx_and_insufficient(self):
        t = self.tmp / "t5.jsonl"; now = time.time(); c = self.tmp / "c5.json"; c.write_text(json.dumps(dict(self.ctl_now(), arm="sys1", canary_pct=100, trace=str(t))))
        t.write_text("".join(json.dumps({"ts": now - 5, "used": "sys1", "sys1_total_ms": 500 + i}) + "\n" for i in range(30)))
        r = run(ROOT / "rollback_check.py", "--control", c); self.assertEqual(r.returncode, 10); self.assertIn("R2", r.stdout)
        t.write_text("".join(json.dumps({"ts": now - 5, "used": "sys1", "sys1_total_ms": 50}) + "\n" for i in range(5)))
        self.assertEqual(run(ROOT / "rollback_check.py", "--control", c).returncode, 2)
        h = self.tmp / "http.jsonl"
        h.write_text("".join(json.dumps({"ts": now - 1000 - i, "status": 200}) + "\n" for i in range(100)) + "".join(json.dumps({"ts": now - 5, "status": 503 if i % 10 == 0 else 200}) + "\n" for i in range(40)))
        r = run(ROOT / "rollback_check.py", "--control", c, "--http-log", h); self.assertEqual(r.returncode, 10); self.assertIn("R3", r.stdout)
        self.assertEqual(json.loads(c.read_text())["arm"], "sys1", "without --apply nothing is changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
