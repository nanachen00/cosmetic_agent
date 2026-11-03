#!/usr/bin/env python3
"""T4 單元測試（stdlib unittest）。在 gateB-colab 目錄執行：python3 -m unittest discover -s sys1/train/tests -v
np-tiny 測試不需要 torch；HF 測試在沒有 torch/transformers 時自動 skip（VM 上會跑）。"""
import json, os, subprocess, sys, tempfile, time, unittest, urllib.request

T = os.path.dirname(os.path.abspath(__file__)); TRAIN = os.path.dirname(T); SYS1 = os.path.dirname(TRAIN); ROOT = os.path.dirname(SYS1)
sys.path.insert(0, TRAIN)
import catalog as C  # noqa: E402
import gateb as GB   # noqa: E402

PY = [sys.executable, "-X", "utf8", "-I"]          # -X utf8: same file/stdio encoding on Windows as on the VM
try:
    import torch, transformers  # noqa: F401
    HAVE_TORCH = True
except Exception:
    HAVE_TORCH = False


def wr(p, s, mode="w"):
    with open(p, mode, encoding="utf-8") as f: f.write(s)


def run(*a, **kw): return subprocess.run(PY + list(a), cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def post(port, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/systemone", json.dumps(body, ensure_ascii=False).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())


class Shared:
    tmp = None; data = None; ckpt = {}

    @classmethod
    def setup(cls):
        if cls.tmp: return
        cls.tmp = tempfile.mkdtemp(prefix="sys1t-"); cls.data = os.path.join(cls.tmp, "data")
        r = run(os.path.join(TRAIN, "make_train_data.py"), "--out", cls.data, "--per", "60")
        assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def train(cls, base):
        cls.setup()
        if base not in cls.ckpt:
            out = os.path.join(cls.tmp, f"ck-{base}"); r = run(os.path.join(TRAIN, "train_head.py"), "--base", base, "--data", cls.data, "--seed", "13", "--out", out, "--device", "cpu", timeout=3600)
            assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
            cls.ckpt[base] = out
        return cls.ckpt[base]


class TestCatalog(unittest.TestCase):
    def test_bundles_fully_supported(self):
        """gateb.pk_bundle 與 kevlib.production_bundle 對所有 gate_data/router_gold 句子產生的題組都能對應到頭（只檢查題目接線，不看標註）。"""
        import gate_data as D, holdout as HO
        for c in D.build(7, 24) + HO.cases():
            p2 = C.P2.parse2(c["text"])
            for q in (GB.pk_bundle(p2), C.K.production_bundle(p2, C.P2.ING_ZH, True)):
                plan, errs = C.plan_questions(q); self.assertEqual(errs, [], c["text"]); self.assertEqual(len(plan), len(q))

    def test_rejects_unknown_and_modified(self):
        _, e = C.plan_questions({"route": {"type": "choice", "instructions": "x", "criteria": {"a": "b"}}}); self.assertTrue(e)
        q = dict(C.K.SAFETY_Q); q["instructions"] = q["instructions"] + "（改過）"; _, e = C.plan_questions({"safety": q}); self.assertTrue(e)
        _, e = C.plan_questions({"intent_choice": C.K.intent_choice_q()}); self.assertTrue(e)
        _, e = C.plan_questions({}); self.assertTrue(e)

    def test_emergency_question_is_gateb(self):
        self.assertEqual(C.emergency_q(), GB.EMERGENCY_Q)

    def test_pair_labels(self):
        c = {"text": "之前買過1600元那瓶太貴了，這次想找乳液，錢控制在900元內", "budget": {"value": 900, "role": "upper"}, "avoid": []}
        labs = [(h, a, C.ROLE_LABELS[l]) for h, a, l in C.pair_examples(c) if h == "role"]
        self.assertIn("other", [x[2] for x in labs]); self.assertIn("upper", [x[2] for x in labs])
        c = {"text": "精華液請避開視黃醇，不要超過600元", "budget": {"value": 600, "role": "upper"}, "avoid": ["retinol"]}
        self.assertIn(("avoid", "A酸", 1.0), C.pair_examples(c))


class TestData(unittest.TestCase):
    def test_exclusion_blocks_cal_sentence(self):
        import gate_data as D
        cal = [c for c in D.build(7, 24) if c["split"] == "cal"][0]; d = tempfile.mkdtemp()
        inj = os.path.join(d, "inj.jsonl"); wr(inj, json.dumps({"text": " " + cal["text"] + "！", "intents": cal["intents"]}, ensure_ascii=False) + "\n")
        r = run(os.path.join(TRAIN, "make_train_data.py"), "--out", os.path.join(d, "o"), "--per", "10", "--inject", inj)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr); self.assertFalse(os.path.exists(os.path.join(d, "o", "train.jsonl")))

    def test_exclusion_blocks_holdout_sentence(self):
        import holdout as HO
        d = tempfile.mkdtemp(); inj = os.path.join(d, "inj.jsonl")
        wr(inj, json.dumps({"text": HO.cases()[5]["text"]}, ensure_ascii=False) + "\n")
        r = run(os.path.join(TRAIN, "make_train_data.py"), "--out", os.path.join(d, "o"), "--per", "10", "--inject", inj)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_synth_from_chinese_model_refused(self):
        d = tempfile.mkdtemp(); s = os.path.join(d, "s.jsonl")
        wr(s, json.dumps({"text": "這是一個合成句子用來測試", "generator": "Qwen2.5-72B-Instruct"}, ensure_ascii=False) + "\n")
        r = run(os.path.join(TRAIN, "make_train_data.py"), "--out", os.path.join(d, "o"), "--per", "10", "--synth", s)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        wr(s, json.dumps({"text": "這是一個合成句子用來測試"}, ensure_ascii=False) + "\n")
        r = run(os.path.join(TRAIN, "make_train_data.py"), "--out", os.path.join(d, "o2"), "--per", "10", "--synth", s)
        self.assertNotEqual(r.returncode, 0)

    def test_train_refuses_tampered_data(self):
        Shared.setup(); d = tempfile.mkdtemp(); import shutil
        shutil.copytree(Shared.data, os.path.join(d, "data")); wr(os.path.join(d, "data", "train.jsonl"), "\n", "a")
        r = run(os.path.join(TRAIN, "train_head.py"), "--base", "np-tiny", "--data", os.path.join(d, "data"), "--seed", "1", "--out", os.path.join(d, "ck"))
        self.assertNotEqual(r.returncode, 0); self.assertIn("SHA256SUMS", r.stdout + r.stderr)


class _ServeBase:
    base = None; port = None

    @classmethod
    def setUpClass(cls):
        ck = Shared.train(cls.base); cls.name = f"{cls.base}-t"
        cls.p = subprocess.Popen(PY + [os.path.join(TRAIN, "serve_sys1.py"), "--ckpt", ck, "--name", cls.name, "--port", str(cls.port), "--device", "cpu"], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(240):
            try: urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/v1/models", timeout=1); break
            except Exception: time.sleep(0.5)
        else: raise RuntimeError(cls.p.stderr.read().decode()[-2000:])

    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(); cls.p.stderr.close()

    def ask(self, text, q): return post(self.port, {"state": text, "model": self.name, "questions": q})

    def test_contract_and_ranges(self):
        text = "孕婦可以用水楊酸嗎，預算八百"; q = GB.pk_bundle(C.P2.parse2(text)); code, j = self.ask(text, q)
        self.assertEqual(code, 200, j); self.assertEqual(set(j["answers"]), set(q)); self.assertIsInstance(j["latency_ms"], float)
        for n, v in j["answers"].items():
            if q[n]["type"] == "noul": self.assertTrue(0 <= v["noul"] <= 1)
            else:
                self.assertAlmostEqual(sum(v["probabilities"].values()), 1.0, places=5); self.assertEqual(set(v["probabilities"]), set(q[n]["criteria"]))
                self.assertIn(v["choice"], q[n]["criteria"]); self.assertTrue(0 <= v["confidence"] <= 1)

    def test_direction_true_high(self):
        """gateb 的 noul 方向探測句：安全句的平均 p 必須高於非安全句（noul 高 = 是）。"""
        ps = lambda S: [self.ask(s, {"safety": C.K.SAFETY_Q})[1]["answers"]["safety"]["noul"] for s in S]
        t, f = ps(GB.TRUE_S), ps(GB.FALSE_S); self.assertGreater(sum(t) / len(t), sum(f) / len(f), (t, f))

    def test_unknown_question_422(self):
        code, j = self.ask("你好", {"route": {"type": "choice", "instructions": "x", "criteria": {"a": "b"}}}); self.assertEqual(code, 422); self.assertIn("route", json.dumps(j, ensure_ascii=False))

    def test_wrong_model_400(self):
        code, _ = post(self.port, {"state": "你好", "model": "kev-latest", "questions": {"safety": C.K.SAFETY_Q}}); self.assertEqual(code, 400)


class TestServeNp(_ServeBase, unittest.TestCase):
    base = "np-tiny"; port = 8236


@unittest.skipUnless(HAVE_TORCH, "torch/transformers 未安裝（VM 上執行）")
class TestServeHfTiny(_ServeBase, unittest.TestCase):
    base = "tiny-random-bert"; port = 8237


class TestGuards(unittest.TestCase):
    def test_port_guard(self):
        for host, port in (("127.0.0.1", 8010), ("127.0.0.1", 8200), ("0.0.0.0", 8232), ("127.0.0.1", 8231)):
            r = run(os.path.join(TRAIN, "serve_sys1.py"), "--ckpt", "/nonexistent", "--name", "x", "--host", host, "--port", str(port))
            self.assertNotEqual(r.returncode, 0); self.assertIn("REFUSED", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
