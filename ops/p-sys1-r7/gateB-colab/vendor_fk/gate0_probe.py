#!/usr/bin/env python3
"""Gate 0 — Kev capability + latency probe (shadow Kev only: 127.0.0.1:8230-8239). ~30 min on the real Kev, seconds on the mock.
Answers: does a request with many questions work, how does latency scale with #questions / #options, is `noul` high=true,
is it deterministic, what happens at concurrency 1/4/8/16/32, and how are bad inputs handled.
Writes KEV_FACTS.json (consumed by gateA) + SUMMARY.md.  Verdict is rule-based, thresholds below (edit with --max-*)."""
from __future__ import annotations
import argparse, json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kevlib as K, gaterun as G, parser_v2 as P2

TRUE_S = ["孕婦可以用水楊酸嗎", "A酸和果酸可以一起用嗎", "用了這個會不會過敏", "哺乳期能擦視黃醇嗎", "敏感肌用果酸安全嗎"]
FALSE_S = ["推薦一款保濕乳液", "你好", "幫我記錄今天的膚況", "預算八百想找防曬", "菸鹼醯胺有什麼功效"]
SENT = ["我是油性肌，想找控油洗面乳，預算 800 以內", "孕婦可以用水楊酸嗎？另外推薦一款洗面乳", "幫我記錄今天臉頰脫皮，順便推薦乳液，預算一千以內",
        "為什麼我的臉一直脫皮", "你好", "50ml 的乳液 800 元", "不要A酸，預算最高1200元", "敏感肌可以一起用水楊酸和A酸嗎"]


def qset(n):
    base = [("safety", K.SAFETY_Q)] + [(f"i_{l}", K.intent_noul_q(l)) for l in K.INTENT_LABELS] + [("concern", K.concern_q()), ("intent_choice", K.intent_choice_q()),
            ("role_0", K.role_q("800元", 800)), ("avoid_0", K.avoid_q("A酸")), ("role_1", K.role_q("500", 500)), ("avoid_1", K.avoid_q("果酸"))]
    return dict(base[:n])


def timed(cl, text, q, reps=1):
    w, k, bad = [], [], 0
    for _ in range(reps):
        r = cl.ask(text, q)
        if r["status"] != 200 or not r["answers"] or set(r["answers"]) != set(q): bad += 1; continue
        w.append(r["wall_ms"]); k.append(r["kev_ms"])
    return {"wall": G.lat(w), "kev": G.lat(k), "bad": bad}


def ladder(cl_factory, level, total, qs, texts, bar=None):
    idx = [0]; lk = threading.Lock(); W, KV, ERR = [], [], [0]; per = {}
    def worker(vu):
        cl = cl_factory(); mine = []
        while True:
            with lk:
                if idx[0] >= total: break
                i = idx[0]; idx[0] += 1
            r = cl.ask(texts[i % len(texts)] + ("" if i < len(texts) else f"（{i}）"), qs)
            if r["status"] != 200 or not r["answers"]: ERR[0] += 1; continue
            mine.append(r["wall_ms"]); W.append(r["wall_ms"]); KV.append(r["kev_ms"])
            if bar:
                with lk: bar.update(len(W) + ERR[0])
        per[vu] = G.lat(mine)
    t0 = time.perf_counter(); th = [threading.Thread(target=worker, args=(v,)) for v in range(level)]
    [t.start() for t in th]; [t.join() for t in th]; el = time.perf_counter() - t0
    w, kv = G.lat(W), G.lat(KV)
    return {"level": level, "n": len(W), "errors": ERR[0], "rps": round(len(W) / el, 2) if el else None, "wall_ms": w, "kev_ms": kv,
            "queue_est_ms_p50": (w["p50"] - kv["p50"]) if w["p50"] is not None and kv["p50"] is not None else None, "per_vu_p50": {v: x["p50"] for v, x in sorted(per.items())}}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--base-url", default="http://127.0.0.1:8230"); ap.add_argument("--out"); ap.add_argument("--resume", action="store_true")
    ap.add_argument("--reps", type=int, default=20); ap.add_argument("--levels", default="1,4,8,16,32"); ap.add_argument("--requests-per-level", type=int, default=160)
    ap.add_argument("--facts-out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "KEV_FACTS.json")); ap.add_argument("--max-p95-ms-c1", type=float, default=500); ap.add_argument("--max-p95-ms-c8", type=float, default=1500)
    A = ap.parse_args()
    cl = K.KevClient(A.base_url); fac = lambda: K.KevClient(A.base_url)
    run = G.Run("gate0", A.out, A.resume, nsteps=9)
    def render(r):
        d = r.done; L = [f"# Gate 0 — Kev capability / latency  ({'partial' if len(d) < 8 else 'complete'})\n", f"Target `{A.base_url}` (shadow Kev). Steps done: {', '.join(d) or 'none'}\n"]
        if "contract" in d: L.append(f"- contract: models={d['contract']['models_status']} single-choice={d['contract']['choice_ok']} single-noul={d['contract']['noul_ok']}")
        if "noul_direction" in d: n = d["noul_direction"]; L.append(f"- noul direction: mean(true-set)={n['mean_true']:.3f} mean(false-set)={n['mean_false']:.3f} -> **{'high = TRUE' if n['true_high'] else 'high = FALSE (inverted; handled by KEV_FACTS)'}**  separation={n['separation']:.3f}")
        if "multi_question" in d:
            L.append("\n## latency vs #questions per request (kev_ms p50 / wall p50 / bad)\n\n|Q|kev p50|wall p50|bad|\n|--|--|--|--|")
            for row in d["multi_question"]["rows"]: L.append(f"|{row['q']}|{row['kev']['p50']:.1f}|{row['wall']['p50']:.1f}|{row['bad']}|")
        if "options" in d: L.append("\n## options per choice question\n\n" + " · ".join(f"{o['opts']} opts: kev p50 {o['kev']['p50']:.1f} ms" for o in d["options"]["rows"]))
        if "determinism" in d: x = d["determinism"]; L.append(f"\n- determinism: {x['identical']}/{x['n']} identical answers across repeats, max prob drift {x['max_drift']:.4f}")
        if "state_cache" in d: x = d["state_cache"]; L.append(f"- new vs repeated state: new p50 {x['new']['kev']['p50']:.1f} ms vs repeat p50 {x['repeat']['kev']['p50']:.1f} ms")
        if "limits" in d: L.append("- input handling: " + "; ".join(f"{k}→{v}" for k, v in d["limits"]["cases"].items()))
        if "concurrency" in d:
            L.append("\n## concurrency ladder (production-style bundle, closed loop)\n\n|VU|n|err|rps|wall p50|wall p95|wall p99|kev p50|queue est p50|\n|--|--|--|--|--|--|--|--|--|")
            for x in d["concurrency"]["rows"]: L.append(f"|{x['level']}|{x['n']}|{x['errors']}|{x['rps']}|{x['wall_ms']['p50']:.0f}|{x['wall_ms']['p95']:.0f}|{x['wall_ms']['p99']:.0f}|{x['kev_ms']['p50']:.0f}|{(x['queue_est_ms_p50'] or 0):.0f}|")
        if "verdict" in d:
            L.append("\n## verdict\n"); L += [f"- **{k}**: {v}" for k, v in d["verdict"]["checks"].items()]; L.append(f"\n**GATE 0: {d['verdict']['result']}**")
        return "\n".join(L) + "\n"
    run.render = render

    def body():
        def contract():
            m = cl.models(); c = cl.ask("推薦一款乳液", {"r": K.intent_choice_q()}); n = cl.ask("孕婦可以用嗎", {"s": K.SAFETY_Q})
            ok_c = bool(c["answers"]) and "choice" in c["answers"]["r"] and bool(c["answers"]["r"].get("probabilities"))
            ok_n = bool(n["answers"]) and "noul" in n["answers"]["s"]
            return {"models_status": m["status"], "choice_ok": ok_c, "noul_ok": ok_n, "choice_sample": (c["answers"] or {}).get("r"), "noul_sample": (n["answers"] or {}).get("s")}
        ct = run.step("contract", contract)
        if not (ct["choice_ok"] and ct["noul_ok"]): raise SystemExit("[GATE0 FAIL] Kev did not return choice/noul in the expected shape — see done.json")
        def direction():
            K.NOUL_TRUE_HIGH = True; t = [K.p_noul(cl.ask(s, {"safety": K.SAFETY_Q})["answers"], "safety") for s in TRUE_S]; f = [K.p_noul(cl.ask(s, {"safety": K.SAFETY_Q})["answers"], "safety") for s in FALSE_S]
            mt, mf = sum(t) / len(t), sum(f) / len(f)
            return {"mean_true": mt, "mean_false": mf, "true_high": mt >= mf, "separation": abs(mt - mf), "true_set": t, "false_set": f}
        dr = run.step("noul_direction", direction)
        def mq():
            rows = []
            for n in (1, 3, 5, 8, 12):
                q = qset(n); rows.append(dict(q=n, **timed(cl, SENT[0], q, A.reps)))
            return {"rows": rows}
        run.step("multi_question", mq)
        def opts():
            rows = []
            for n in (2, 6, 14):
                crit = {f"選項{i}": f"第{i}種需求" for i in range(n)}
                rows.append(dict(opts=n, **timed(cl, SENT[0], {"c": K.choice("選一個", crit)}, A.reps)))
            return {"rows": rows}
        run.step("options", opts)
        def determ():
            q = qset(8); outs = [cl.ask(SENT[1], q)["answers"] for _ in range(10)]
            ident = sum(1 for o in outs if json.dumps(o, sort_keys=True) == json.dumps(outs[0], sort_keys=True)); drift = 0.0
            for o in outs[1:]:
                for k in q:
                    a, b = K.p_noul(o, k) if "noul" in (o[k] or {}) else None, K.p_noul(outs[0], k) if "noul" in (outs[0][k] or {}) else None
                    if a is not None and b is not None: drift = max(drift, abs(a - b))
            return {"n": 10, "identical": ident, "max_drift": drift}
        run.step("determinism", determ)
        def cache():
            q = qset(8); w = []
            for i in range(A.reps): w.append(cl.ask(f"{SENT[i % len(SENT)]}（新{time.time_ns()}）", q))
            rep = timed(cl, SENT[2], q, A.reps)
            nk = [x["kev_ms"] for x in w if x["answers"]]; nw = [x["wall_ms"] for x in w if x["answers"]]
            return {"new": {"kev": G.lat(nk), "wall": G.lat(nw)}, "repeat": rep}
        run.step("state_cache", cache)
        def limits():
            q = {"s": K.SAFETY_Q}; cs = {}
            cs["empty_state"] = cl.ask("", q)["status"]; cs["long_2000"] = cl.ask("保養" * 1000, q)["status"]
            cs["emoji"] = cl.ask("😀😀 推薦乳液 🧴", q)["status"]
            r = cl.request("POST", "/v1/systemone", {"state": "x", "model": "kev-latest", "questions": {"s": {"type": "bogus", "instructions": "x", "criteria": {}}}}); cs["bad_type"] = r["status"]
            r = cl.request("POST", "/v1/systemone", {"state": "x", "model": "kev-latest", "questions": {}}); cs["no_questions"] = r["status"]
            return {"cases": cs}
        run.step("limits", limits)
        def conc():
            texts = SENT; rows = []
            for lv in [int(x) for x in A.levels.split(",")]:
                if run.interrupted: break
                qs = K.production_bundle(P2.parse2(texts[0]), P2.ING_ZH, need_concern=True)
                tot = max(A.requests_per_level, lv * 8); bar = G.Bar(tot, f"  VU={lv}")
                rows.append(ladder(fac, lv, tot, qs, texts, bar)); bar.done(); print(f"    VU={lv}: p95 wall {rows[-1]['wall_ms']['p95']:.0f} ms err {rows[-1]['errors']}", flush=True)
            return {"rows": rows}
        cc = run.step("concurrency", conc)
        def verdict():
            d = run.done; chk = {}
            chk["multi-question works"] = "PASS" if all(r["bad"] == 0 for r in d["multi_question"]["rows"]) else "FAIL (bad answers in some Q counts)"
            chk["noul separation"] = "PASS" if d["noul_direction"]["separation"] >= 0.3 else f"WEAK ({d['noul_direction']['separation']:.2f})"
            r1 = next((r for r in cc["rows"] if r["level"] == 1), None); r8 = next((r for r in cc["rows"] if r["level"] == 8), None)
            chk["c=1 p95"] = "n/a" if not r1 else ("PASS" if r1["wall_ms"]["p95"] <= A.max_p95_ms_c1 else f"SLOW ({r1['wall_ms']['p95']:.0f} ms > {A.max_p95_ms_c1:.0f})")
            chk["c=8 p95"] = "n/a" if not r8 else ("PASS" if r8["wall_ms"]["p95"] <= A.max_p95_ms_c8 else f"SLOW ({r8['wall_ms']['p95']:.0f} ms > {A.max_p95_ms_c8:.0f})")
            chk["no errors"] = "PASS" if all(r["errors"] == 0 for r in cc["rows"]) else "FAIL (errors under load)"
            res = "PASS" if all(v.startswith(("PASS", "n/a")) for v in chk.values()) else "REVIEW"
            facts = {"noul_true_high": d["noul_direction"]["true_high"], "catalogue_signature": K.catalogue_signature(), "ts": time.strftime("%F %T"),
                     "p50_kev_ms_by_q": {r["q"]: r["kev"]["p50"] for r in d["multi_question"]["rows"]}, "base_url": A.base_url}
            json.dump(facts, open(A.facts_out, "w"), indent=1)
            json.dump(facts, open(os.path.join(run.dir, "KEV_FACTS.json"), "w"), indent=1)
            return {"checks": chk, "result": res}
        run.step("verdict", verdict)
    return G.main_guard(run, body)


if __name__ == "__main__":
    sys.exit(main())
