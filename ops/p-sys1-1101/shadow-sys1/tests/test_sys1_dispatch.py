"""T6 tests: shadowpatch guards (sys1 copy), Gate B parity (overlay == gateb/gateA/holdout), overlay integration on the fixture
orchestrator against a mock Sys1.  Never reads or writes production paths; servers only on 127.0.0.1:8234/8235 (8238 must be free: used as the "Sys1 down" port).
Run:  python3 -W ignore tests/test_sys1_dispatch.py   (from shadow-sys1/)"""
import hashlib, importlib, importlib.util, json, os, random, shutil, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]; GB_DIR = ROOT.parent / "gateB-colab"
os.environ["SHADOWPATCH_STATE"] = tempfile.mkdtemp(prefix="s1state-")
os.environ["SHADOWPATCH_PROD_FILE"] = str(Path(tempfile.mkdtemp()) / "prod_roots.json")     # tests never read the real file
SP = [sys.executable, str(ROOT / "shadowpatch.py")]
SPEC = str(ROOT / "patches" / "sys1-dispatch-v1.json")
MOCK_PORT, BAD_PORT, DOWN_PORT = 8235, 8234, 8238


def sp(*a, env=None):
    e = dict(os.environ); e.update(env or {}); return subprocess.run(SP + list(a), capture_output=True, text=True, env=e)


def load_overlay():
    spec = importlib.util.spec_from_file_location("sys1_dispatch_under_test", str(ROOT / "payload" / "sys1_dispatch.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def frozen_file(d: Path, th=None, tamper=False):
    th = th or json.load(open(GB_DIR / "holdout" / "frozen_thresholds.json", encoding="utf-8"))["by_kev"]["0.8b"]["v2"]
    body = {"cand": "test-cand", "thresholds": th, "sha256": hashlib.sha256(json.dumps(th, sort_keys=True).encode()).hexdigest()}
    if tamper: body["thresholds"] = dict(th, emergency=0.01)
    p = d / ("frozen-bad.json" if tamper else "frozen.json"); p.write_text(json.dumps(body, ensure_ascii=False)); return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="s1t-")); self.sh = self.tmp / "shadow" / "cosmetic-agent"
        r = sp("init-shadow", "--src", str(ROOT / "tests" / "fixture" / "cosmetic-agent"), "--dest", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.orch = self.sh / "agents/root/orchestrator.py"; self.orig = self.orch.read_bytes()

    def tearDown(self):
        sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force"); shutil.rmtree(self.tmp, ignore_errors=True)


class Guards(Base):
    def test_apply_revert_identical_and_dirs_removed(self):
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.sh / "agents/root/sys1_vendor/payload/kev_slot_parser.py").is_file())
        mk = json.loads((self.sh / ".sys1-shadow-overlay").read_text()); self.assertIn("prod_roots", mk); self.assertTrue(Path(mk["control"]).as_posix().endswith("control/sys1_dispatch.json"))
        self.assertEqual(sp("verify", "--spec", SPEC, "--shadow-root", str(self.sh)).returncode, 0)
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orch.read_bytes(), self.orig); self.assertFalse((self.sh / "agents/root/sys1_vendor").exists()); self.assertFalse((self.sh / ".sys1-shadow-overlay").exists())

    def test_refuse_production_root_env(self):
        fake = self.tmp / "prod" / "cosmetic-agent"; shutil.copytree(self.sh, fake)
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(fake), env={"SHADOWPATCH_PROD_ROOTS": str(fake)})
        self.assertNotEqual(r.returncode, 0); self.assertIn("G1", r.stderr + r.stdout); self.assertEqual((fake / "agents/root/orchestrator.py").read_bytes(), self.orig)

    def test_refuse_production_root_from_prod_roots_json(self):
        pf = self.tmp / "prod_roots.json"; pf.write_text(json.dumps({"prod_roots": [str(self.tmp / "shadow")]}))
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh), env={"SHADOWPATCH_PROD_FILE": str(pf)})
        self.assertNotEqual(r.returncode, 0); self.assertIn("G1", r.stderr + r.stdout); self.assertEqual(self.orch.read_bytes(), self.orig)

    def test_refuse_tree_with_kev_patch(self):
        self.orch.write_bytes(self.orig + b"\n# SHADOW-KEV-PATCH\n")
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertNotEqual(r.returncode, 0); self.assertIn("shadow patch tag", r.stderr + r.stdout)

    def test_anchor_missing_or_duplicated_refused(self):
        for bad in (self.orig.replace(b"elif len(user_input) > 20:", b"elif len(user_input) > 21:"),
                    self.orig + b"\n\ndef _dup(self, user_input):\n    if 1:\n        pass\n        elif_ = 0\n" + b"        elif len(user_input) > 20:\n            pain_gain_started = time.perf_counter()\n"):
            self.orch.write_bytes(bad); r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh))
            self.assertNotEqual(r.returncode, 0); self.assertIn("G5", r.stderr + r.stdout); self.assertEqual(self.orch.read_bytes(), bad)
            self.assertFalse((self.sh / "agents/root/sys1_dispatch.py").exists())

    def test_unmarked_refused(self):
        d = self.tmp / "plain"; shutil.copytree(self.sh, d); (d / ".shadow-copy").unlink()
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(d)); self.assertNotEqual(r.returncode, 0); self.assertIn("G2", r.stderr + r.stdout)

    def test_applies_to_real_production_orchestrator_copy(self):
        """Anchors + compile check on a shadow copy of the vendored PRODUCTION orchestrator (gateB-colab/vendor_prod, read-only source)."""
        dst = self.tmp / "shadow" / "vp"; r = sp("init-shadow", "--src", str(GB_DIR / "vendor_prod"), "--dest", str(dst)); self.assertEqual(r.returncode, 0, r.stderr)
        before = (GB_DIR / "vendor_prod/agents/root/orchestrator.py").read_bytes()
        r = sp("apply", "--spec", SPEC, "--shadow-root", str(dst)); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((dst / "agents/root/orchestrator.py").read_text().count("SHADOW-SYS1-PATCH"), 6)
        r = sp("revert", "--spec", SPEC, "--shadow-root", str(dst)); self.assertEqual(r.returncode, 0)
        self.assertEqual((dst / "agents/root/orchestrator.py").read_bytes(), before); self.assertEqual((GB_DIR / "vendor_prod/agents/root/orchestrator.py").read_bytes(), before)


class RevertTwiceSameSecond(Base):
    def test_two_apply_revert_cycles_fast(self):
        for _ in range(3):
            self.assertEqual(sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)).returncode, 0)
            r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orch.read_bytes(), self.orig)


class Parity(unittest.TestCase):
    """The overlay's decision code must equal the Gate B code that S2 scored (gateb.decide_v2 / gateA.kev_view / holdout.route_of)."""
    @classmethod
    def setUpClass(cls):
        sys.path[:0] = [str(GB_DIR), str(GB_DIR / "vendor_fk"), str(GB_DIR / "vendor_fk" / "tests")]
        import gateb, gateA, holdout, gate_data, extra_safety, kevlib
        cls.GB, cls.GA, cls.HO, cls.K = gateb, gateA, holdout, kevlib
        cls.cases = gate_data.build(7, 24) + extra_safety.cases() + holdout.cases(); cls.O = load_overlay()
        cls.th = json.load(open(GB_DIR / "holdout" / "frozen_thresholds.json", encoding="utf-8"))["by_kev"]["0.8b"]["v2"]

    def test_emergency_question_and_bundle(self):
        self.assertEqual(self.O._EMERGENCY_Q, self.GB.EMERGENCY_Q)
        for c in self.cases:
            p2 = self.GB.P2.parse2(c["text"]); self.assertEqual(self.O.pk_bundle(self.O._vendor()["P2"].parse2(c["text"])), self.GB.pk_bundle(p2), c["text"])

    def test_decisions_identical(self):
        import mock_kev
        A = type("A", (), {"err_rate": 0.1, "invert_noul": False})(); rng = random.Random(5); n = 0
        for c in self.cases:
            p2g = self.GB.P2.parse2(c["text"]); p2o = self.O._vendor()["P2"].parse2(c["text"]); self.assertEqual(p2g, p2o)
            q = self.GB.pk_bundle(p2g)
            answer_sets = [{k: mock_kev.answer(c["text"], k, v, A) for k, v in q.items()}]
            for _ in range(3):
                ans = {}
                for k, v in q.items():
                    if v["type"] == "noul": ans[k] = {"noul": rng.random()}
                    else: ch = rng.choice(list(v["criteria"])); ans[k] = {"choice": ch, "probabilities": {ch: 1.0}, "confidence": 1.0}
                answer_sets.append(ans)
            answer_sets.append({})                                        # Sys1 returned nothing -> Parser only inside decide
            for ans in answer_sets:
                kvg = self.GA.kev_view(c, {"answers": ans}, p2g, None); pe = self.K.p_noul(ans, "emergency")
                dg = self.GB.decide_v2(c, p2g, kvg, pe, self.th); do = self.O.decide_v2(p2o, self.O.kev_view(ans, p2o), self.O._p_noul(ans, "emergency"), self.th)
                for k in ("safety", "intents", "budget", "avoid", "concern", "emergency"): self.assertEqual(dg[k], do[k], (c["text"], k))
                self.assertEqual(self.HO.route_of(dg), self.O.route_of(do)); n += 1
        self.assertGreater(n, 2000)


class _Bad(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"])); b = b'{"answers": {"safety": {"noul": 7}}, "latency_ms": 1}'
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


class Functional(Base):
    @classmethod
    def setUpClass(cls):
        try: import pydantic  # noqa
        except ImportError: raise unittest.SkipTest("pydantic not installed")
        cls.kev = subprocess.Popen([sys.executable, str(GB_DIR / "vendor_fk/tests/mock_kev.py"), "--port", str(MOCK_PORT)])
        cls.bad = ThreadingHTTPServer(("127.0.0.1", BAD_PORT), _Bad); threading.Thread(target=cls.bad.serve_forever, daemon=True).start()
        for _ in range(50):
            try: urllib.request.urlopen(f"http://127.0.0.1:{MOCK_PORT}/v1/models", timeout=0.5); break
            except Exception: time.sleep(0.1)

    @classmethod
    def tearDownClass(cls): cls.kev.terminate(); cls.kev.wait(); cls.bad.shutdown()

    def load(self, arm, extra=None, apply=True):
        if apply: r = sp("apply", "--spec", SPEC, "--shadow-root", str(self.sh)); self.assertEqual(r.returncode, 0, r.stderr)
        self.ctl = self.tmp / "ctl.json"; self.trace = self.tmp / "trace.jsonl"
        self.write_ctl(dict({"arm": arm, "canary_pct": 100, "trace": str(self.trace), "trace_text": True, "sys1_url": f"http://127.0.0.1:{MOCK_PORT}",
                             "model": "mock", "frozen": str(frozen_file(self.tmp)), "timeout_s": 2}, **(extra or {})))
        os.environ["SYS1_DISPATCH_CONTROL"] = str(self.ctl)
        for m in [m for m in sys.modules if m.split(".")[0] in ("agents", "tools", "schemas")]: del sys.modules[m]
        sys.path.insert(0, str(self.sh)); self.addCleanup(lambda: sys.path.remove(str(self.sh)))
        mod = importlib.import_module("agents.root.orchestrator"); ip = importlib.import_module("tools.agent.intent_parser"); ip.CALLS.clear()
        self.ov = importlib.import_module("agents.root.sys1_dispatch")
        return mod.Orchestrator(), ip

    def write_ctl(self, d):
        self.ctl.write_text(json.dumps(d, ensure_ascii=False)); t = time.time() + random.random() * 5 + 1; os.utime(self.ctl, (t, t))

    def rows(self): return [json.loads(l) for l in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_dispatch_table_no_llm(self):
        o, ip = self.load("sys1")
        cases = {"請推薦預算1000元以下的乳液，我是乾性肌": ("dispatch", "product_agent:dry:乳液:1000"), "玻尿酸和神經醯胺有什麼差別？": ("dispatch", "knowledge_agent"),
                 "孕婦可以用水楊酸嗎": ("dispatch", "safety_agent"), "幫我記錄今天兩頰偏乾": ("skin_tracking", None), "那個呢": ("clarify:routing", None)}
        for q, (stage, agent) in cases.items():
            r = o._handle_new_input(q); self.assertEqual(r["stage"], stage, (q, r, self.rows()[-1:]))
            if agent: self.assertEqual(r["agent"], agent, (q, r))
        self.assertEqual(ip.CALLS, [], "LLM parse_intent was called under the sys1 arm")
        rows = self.rows(); self.assertEqual(len(rows), len(cases)); self.assertTrue(all(r["used"] == "sys1" and r["sys1_ms"] is not None for r in rows))

    def test_avoided_ingredient_not_in_query_expansion(self):
        o, ip = self.load("sys1"); o._handle_new_input("想找乳液，不要A酸，預算800元以內")
        self.assertNotIn("retinol", o.state.intent.query_expansion); self.assertEqual(self.rows()[-1]["avoid"], ["retinol"])

    def test_sys1_down_clarify_or_parser_safety_never_llm(self):
        o, ip = self.load("sys1", {"sys1_url": f"http://127.0.0.1:{DOWN_PORT}", "timeout_s": 1})
        self.assertEqual(o._handle_new_input("請推薦乳液")["stage"], "clarify:routing")
        self.assertEqual(o._handle_new_input("孕婦可以用水楊酸嗎")["agent"], "safety_agent")
        self.assertEqual(ip.CALLS, []); w = [r["why"] for r in self.rows()]; self.assertEqual(w, ["sys1_error:clarify", "sys1_error:parser_safety"])

    def test_malformed_answer_is_error(self):
        o, ip = self.load("sys1", {"sys1_url": f"http://127.0.0.1:{BAD_PORT}"})
        self.assertEqual(o._handle_new_input("請推薦乳液")["stage"], "clarify:routing"); self.assertIn("malformed", self.rows()[-1]["sys1_error"]); self.assertEqual(ip.CALLS, [])

    def test_error_fallback_llm_only_when_owner_sets_it(self):
        o, ip = self.load("sys1", {"sys1_url": f"http://127.0.0.1:{DOWN_PORT}", "timeout_s": 1, "error_fallback": "llm"})
        o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)
        self.assertEqual(o._handle_new_input("孕婦可以用水楊酸嗎")["agent"], "safety_agent"); self.assertEqual(len(ip.CALLS), 1, "safety must not go to the LLM")

    def test_arm_off_passthrough_and_switch(self):
        o, ip = self.load("off"); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)
        d = json.loads(self.ctl.read_text()); d["arm"] = "sys1"; self.write_ctl(d); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)
        d["arm"] = "off"; self.write_ctl(d); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 2)

    def test_inert_without_marker_or_under_prod_root(self):
        o, ip = self.load("sys1"); (self.sh / ".sys1-shadow-overlay").unlink(); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)
        sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force")
        pf = self.tmp / "pr.json"; pf.write_text(json.dumps({"prod_roots": []}))
        o, ip = self.load("sys1"); mk = self.sh / ".sys1-shadow-overlay"; m = json.loads(mk.read_text()); m["prod_roots"] = [str(self.sh)]; mk.write_text(json.dumps(m))
        o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1, "overlay must be inert inside a production root")

    def test_bad_config_means_off(self):
        for extra in ({"frozen": str(frozen_file(self.tmp, tamper=True))}, {"model": None}, {"sys1_url": "http://127.0.0.1:8010"}, {"arm": "kev_route"}):
            r = sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force"); self.assertEqual(r.returncode, 0, r.stderr)
            o, ip = self.load("sys1", extra); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1, extra)

    def test_canary(self):
        o, ip = self.load("sys1", {"canary_pct": 0}); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1); self.assertFalse(self.rows()[-1]["canary"])
        d = json.loads(self.ctl.read_text()); d["canary_pct"] = 50; self.write_ctl(d)
        mod = sys.modules["agents.root.orchestrator"]; used = []
        for _ in range(200):
            oo = mod.Orchestrator(); oo.state.session_id = f"s{random.random()}"; oo._handle_new_input("請推薦乳液"); used.append(self.rows()[-1]["used"])
        frac = used.count("sys1") / len(used); self.assertTrue(0.3 < frac < 0.7, frac)
        oo = mod.Orchestrator(); oo.state.session_id = "fixed"; a = [self.ov.in_canary(self.ov._ctl(), oo) for _ in range(5)]; self.assertEqual(len(set(a)), 1)

    def test_shadow_logs_both_and_returns_llm(self):
        o, ip = self.load("shadow")
        for q in ("請推薦預算1000元以下的乳液", "孕婦可以用水楊酸嗎", "幫我記錄今天兩頰偏乾"): o._handle_new_input(q)
        self.assertEqual(len(ip.CALLS), 3)
        for _ in range(50):
            if len(self.rows()) == 3: break
            time.sleep(0.1)
        rows = self.rows(); self.assertEqual(len(rows), 3)
        for r in rows: self.assertEqual(r["used"], "llm"); self.assertIn(r["llm_route"], ("product", "safety", "clarify", "knowledge")); self.assertIn("sys1_route", r)
        self.assertEqual({r["text"]: r["sys1_route"] for r in rows}["孕婦可以用水楊酸嗎"], "safety")

    # ---- regressions from the 2026-10-26 review
    def test_avoid_never_in_query_expansion_on_error_path(self):
        o, ip = self.load("sys1", {"sys1_url": f"http://127.0.0.1:{DOWN_PORT}", "timeout_s": 1})
        r = o._handle_new_input("孕婦可以用乳液嗎，不要水楊酸"); self.assertEqual(r["agent"], "safety_agent")
        self.assertNotIn("salicylic_acid", o.state.intent.query_expansion); self.assertEqual(ip.CALLS, [])

    def test_bad_canary_pct_is_off_not_crash(self):
        for bad in ("half", None, -5, 500):
            sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force")
            o, ip = self.load("sys1", {"canary_pct": bad}); o._handle_new_input("請推薦乳液")
            self.assertEqual(len(ip.CALLS), 1, bad)   # invalid -> arm off; None -> 0% canary; both = LLM path

    def test_malformed_marker_prod_roots_fail_closed(self):
        for bad in ("/var/lib/x", [None], [3], {"a": 1}):
            sp("revert", "--spec", SPEC, "--shadow-root", str(self.sh), "--force")
            o, ip = self.load("sys1"); mk = self.sh / ".sys1-shadow-overlay"; m = json.loads(mk.read_text()); m["prod_roots"] = bad; mk.write_text(json.dumps(m))
            o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1, bad)

    def test_vendor_tamper_is_off_not_crash(self):
        o, ip = self.load("sys1"); v = self.sh / "agents/root/sys1_vendor/parser_v2.py"; v.write_text(v.read_text() + "\n# tampered\n")
        self.ov._mods.clear(); o._handle_new_input("請推薦乳液"); self.assertEqual(len(ip.CALLS), 1)

    def test_safety_flag_survives_thread_hop(self):
        o, ip = self.load("sys1"); out = {}
        t = threading.Thread(target=lambda: out.setdefault("i", self.ov.parse_intent(_orch=o, _fallback=ip.parse_intent, user_query="孕婦可以用水楊酸嗎"))); t.start(); t.join()
        self.assertTrue(self.ov.force_safety(o, out["i"]))
        o2 = sys.modules["agents.root.orchestrator"].Orchestrator(); o2.state.session_id = "other"
        self.assertFalse(self.ov.force_safety(o2, out["i"]), "another session must not inherit the flag")

    def test_role_span_with_newline_supported(self):
        import importlib.util as iu
        sys.path.insert(0, str(GB_DIR / "sys1" / "train")); import catalog as Cat
        plan, errs = Cat.plan_questions({"role_0": Cat.K.role_q("預算\n500元", 500)}); self.assertEqual(errs, []); self.assertEqual(plan[0][2], "預算\n500元")

    def test_canary_keyed_by_user_not_default_conversation(self):
        o, ip = self.load("sys1", {"canary_pct": 50}); mod = sys.modules["agents.root.orchestrator"]; hits = []
        for i in range(200):
            oo = mod.Orchestrator(); oo.user_id, oo.conversation_id = f"guest-{i}", "default"; hits.append(self.ov.in_canary(self.ov._ctl(), oo))
        self.assertTrue(0.3 < sum(hits) / len(hits) < 0.7, sum(hits))


class RealSchema(unittest.TestCase):
    """build_intent against the PRODUCTION schemas/intent.py (gateB-colab/vendor_prod = analysis-source-20261013T163259Z cosmetic-agent)."""
    @classmethod
    def setUpClass(cls):
        try: import pydantic  # noqa
        except ImportError: raise unittest.SkipTest("pydantic not installed")
        for m in [m for m in sys.modules if m.split(".")[0] == "schemas"]: del sys.modules[m]
        sys.path.insert(0, str(GB_DIR / "vendor_prod")); cls.O = load_overlay()
        import schemas.intent as SI; cls.SI = SI

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(GB_DIR / "vendor_prod"))
        for m in [m for m in sys.modules if m.split(".")[0] == "schemas"]: del sys.modules[m]

    def mk(self, text, route, **d):
        p2 = self.O._vendor()["P2"].parse2(text); dec = dict({"safety": route == "safety", "intents": [], "budget": None, "avoid": [], "concern": None}, **d)
        return self.O.build_intent(text, {"route": route, "decision": dec, "p2": p2})

    def test_routes_build_valid_production_intents(self):
        T = self.SI.IntentType
        r = self.mk("我是乾性肌，想找保濕的乳液，預算800元以內", "product", intents=["find_product"], budget={"value": 800, "role": "upper"}, concern="保濕")
        self.assertEqual((r.intent_type, r.budget_range, r.concern, r.skin_type.value, r.product_category.value), (T.FIND_PRODUCT, "800", ["保濕"], "dry", "乳液"))
        self.assertEqual(self.mk("這兩款乳液哪個比較划算", "product", intents=["price_comparison"]).intent_type, T.PRICE_COMPARISON)
        self.assertEqual(self.mk("玻尿酸是什麼", "knowledge", intents=["knowledge"]).intent_type, T.KNOWLEDGE_QUERY)
        s = self.mk("孕婦可以用水楊酸嗎", "safety", intents=["knowledge"]); self.assertEqual((s.intent_type, s.budget_range), (T.FIND_PRODUCT, None))
        for rt in ("clarify", "conversation", "skin_tracking"): self.assertEqual(self.mk("那個呢", rt).intent_type, T.UNCLEAR)
        a = self.mk("想找乳液，不要A酸，預算800元以內", "product", intents=["find_product"], budget={"value": 800, "role": "upper"})
        self.assertNotIn("retinol", a.query_expansion)
        self.assertGreaterEqual(self.SI.calculate_routing_confidence(r), 0.5, "product intent must not be sent to routing clarification")


if __name__ == "__main__":
    unittest.main(verbosity=2)
