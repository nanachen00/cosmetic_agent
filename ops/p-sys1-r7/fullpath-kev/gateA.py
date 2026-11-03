#!/usr/bin/env python3
"""Gate A — module tests of Parser, layer 1 and Kev L1(safety)/L2(intent multi-label)/L3(details) on synthetic labelled data.
Shadow Kev only (127.0.0.1:8230-8239).  One collection pass stores raw Kev answers per sentence (jsonl, resumable); every
metric/threshold is then computed OFFLINE from that file, so re-analysis never needs the model again.
Stages: A1 Parser v1→v2 unit | A2 layer-1 coverage | A3 Kev L1 safety | A4 Kev L2 intents | A5 Kev L3 details | A6 end-to-end + error propagation | A7 latency & calls/turn | A8 walkthrough regression.
Thresholds are tuned on the `cal` split and every reported number is from the `test` split.  Gold is synthetic: this proves wiring/robustness,
not real-user accuracy.  Severe counters = failures that change data or safety behaviour."""
from __future__ import annotations
import argparse, glob, importlib.util, json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kevlib as K, gaterun as G, gate_data as D, parser_v2 as P2
import kev_slot_parser as P1

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = list(K.INTENT_LABELS)
GRID = [round(x * 0.05, 2) for x in range(1, 20)]
PARSER_FLAG_TO_INTENT = [("explicit_product_search", "find_product"), ("explicit_compare", "knowledge"), ("knowledge_marker", "knowledge"),
                         ("explicit_tracking", "record"), ("conversation_marker", "chitchat"), ("ambiguous_marker", "unclear")]


def load_layer1(path):
    cands = [path] if path else [os.path.join(HERE, "..", "prod_dialogue_router.py")] + glob.glob(os.path.join(HERE, "..", "*", "prod_dialogue_router.py")) + ["/home/claude/suite/prod_dialogue_router.py"]
    for p in cands:
        if p and os.path.exists(p):
            spec = importlib.util.spec_from_file_location("prod_router_ro", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            if hasattr(m, "classify_dialogue_route"): return m.classify_dialogue_route, os.path.abspath(p)
    return None, None


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None; r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"p": p, "r": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}


def fmt(x, d=3): return "n/a" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))


def parser_intents(p):
    return sorted({lab for flag, lab in PARSER_FLAG_TO_INTENT if p.get(flag)})


# ----------------------------------------------------------------------------- per-case derivations (pure functions of raw answers)
def kev_view(c, raw, p2, thr):
    a = raw.get("answers") or {}
    ps = K.p_noul(a, "safety"); pi = {l: K.p_noul(a, f"i_{l}") for l in LABELS}
    con = (a.get("concern") or {}).get("choice"); roles = {}
    for i, cand in enumerate(p2["budget_candidates"][:3]):
        r = (a.get(f"role_{i}") or {}).get("choice")
        if r: roles[i] = r
    avoid = {k: K.p_noul(a, f"avoid_{k}") for k in p2["ingredients"][:3] if f"avoid_{k}" in a}
    return {"ok": bool(a), "p_safety": ps, "p_intent": pi, "concern": con, "roles": roles, "p_avoid": avoid, "intent_choice": (a.get("intent_choice") or {}).get("choice")}


def decide(c, p2, kv, th):
    """The proposed production policy (R1-R9 subset) applied to one sentence."""
    safety = bool(p2.get("safety_marker")) or (kv["p_safety"] is not None and kv["p_safety"] >= th["safety"])
    kev_int = {l for l in LABELS if kv["p_intent"].get(l) is not None and kv["p_intent"][l] >= th["intent"].get(l, .5)}
    p_int = set(parser_intents(p2)); intents = sorted(kev_int | p_int) if kv["ok"] else sorted(p_int)
    budget = None; cands = p2["budget_candidates"]
    for i, cand in enumerate(cands[:3]):
        if cand["role"] == "range_lo": continue
        role = {"upper": "upper", "range_hi": "upper", "target": "target"}.get(cand["role"]) or kv["roles"].get(i)
        if role in ("upper", "target") and budget is None: budget = {"value": cand["value"], "role": role}
    avoid = sorted(set(p2["avoid_hint"]) & {k for k, v in kv["p_avoid"].items() if v is not None and v >= th["avoid"]}) if kv["ok"] else sorted(p2["avoid_hint"])
    concern = None if kv["concern"] in (None, K.NO_CONCERN) else kv["concern"]
    return {"safety": safety, "intents": intents, "budget": budget, "avoid": avoid, "concern": concern, "kev_intents": sorted(kev_int), "parser_intents": sorted(p_int)}


def parser_only(c, p2):
    b = None
    if p2["budget_upper"] is not None: b = {"value": p2["budget_upper"], "role": "upper"}
    elif p2["budget_target"] is not None: b = {"value": p2["budget_target"], "role": "target"}
    return {"safety": bool(p2.get("safety_marker")), "intents": parser_intents(p2), "budget": b, "avoid": sorted(p2["avoid_hint"]), "concern": None}


def v1_budget(p1):
    if p1.get("budget_twd_max") is not None: return {"value": p1["budget_twd_max"], "role": "upper"}
    if p1.get("budget_twd_target") is not None: return {"value": p1["budget_twd_target"], "role": "target"}
    return None


def errors(c, out):
    """list of (code, severity) for one decided case vs gold."""
    e = []
    if c["safety"] and not out["safety"]: e.append(("safety_missed_emergency" if c["emergency"] else "safety_missed", "severe"))
    if out["safety"] and not c["safety"]: e.append(("safety_false_alarm", "minor"))
    g, o = set(c["intents"]), set(out["intents"])
    if "record" in g and "record" not in o: e.append(("record_dropped", "severe"))
    if "record" in o and "record" not in g: e.append(("record_false", "severe"))
    if g - o - {"record"}: e.append(("intent_missed", "moderate"))
    if o - g - {"record"}: e.append(("intent_extra", "minor"))
    gb, ob = c["budget"], out["budget"]
    if ob and not gb: e.append(("budget_false", "severe"))
    elif gb and ob and gb["value"] != ob["value"]: e.append(("budget_wrong_value", "severe"))
    elif gb and ob and gb["role"] not in ("any", ob["role"]): e.append(("budget_wrong_role", "moderate"))
    elif gb and not ob: e.append(("budget_missed", "moderate"))
    if set(c["avoid"]) - set(out["avoid"]): e.append(("avoid_missed", "severe"))
    if set(out["avoid"]) - set(c["avoid"]): e.append(("avoid_extra", "moderate"))
    if "find_product" in g and c["concern"] != out.get("concern") and out.get("concern") is not None: e.append(("concern_wrong", "moderate"))
    return e


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--base-url", default="http://127.0.0.1:8230"); ap.add_argument("--out"); ap.add_argument("--resume", action="store_true")
    ap.add_argument("--seed", type=int, default=7); ap.add_argument("--n-per", type=int, default=24); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--vus", type=int, default=4); ap.add_argument("--layer1", default=None); ap.add_argument("--facts", default=os.path.join(HERE, "KEV_FACTS.json")); ap.add_argument("--latency-n", type=int, default=64)
    ap.add_argument("--min-recall-safety", type=float, default=0.99); ap.add_argument("--no-kev", action="store_true", help="offline stages only (A1,A2,A8-parser)")
    A = ap.parse_args()
    K.check_base(A.base_url)                                       # refuse non-shadow targets BEFORE anything is created
    run = G.Run("gateA", A.out, A.resume, nsteps=(3 if A.no_kev else 9))
    cases = D.build(A.seed, A.n_per)
    if A.limit: cases = cases[:A.limit]
    byid = {c["id"]: c for c in cases}
    facts_p = A.facts
    if os.path.exists(facts_p):
        f = json.load(open(facts_p)); K.NOUL_TRUE_HIGH = bool(f.get("noul_true_high", True))
        if f.get("catalogue_signature") != K.catalogue_signature(): print("[warn] KEV_FACTS.json was produced with a different question catalogue — rerun gate0", file=sys.stderr)
    else: print("[warn] KEV_FACTS.json missing — assuming noul high=true; run `fp.sh gate0` first", file=sys.stderr)
    P2c = {c["id"]: P2.parse2(c["text"]) for c in cases}; P1c = {c["id"]: P1.parse(c["text"]).to_dict() for c in cases}
    raw_p = os.path.join(run.dir, "kev_raw.jsonl"); RAW = {}
    if os.path.exists(raw_p):
        for ln in open(raw_p):
            try: j = json.loads(ln); RAW[j["id"]] = j
            except Exception: pass                                     # torn last line after a kill: that case is simply re-collected

    # ---------------------------------------------------------------- rendering
    def render(r):
        d = r.done; L = [f"# Gate A — module tests ({'COMPLETE' if 'A6' in d else 'PARTIAL'})\n",
                         f"cases={len(cases)} (cal={sum(c['split']=='cal' for c in cases)}, test={sum(c['split']=='test' for c in cases)}) · Kev answers collected={len(RAW)} · target `{A.base_url}` · noul_true_high={K.NOUL_TRUE_HIGH}\n",
                         "> Gold is synthetic: proves wiring and robustness, not real-user accuracy. Thresholds tuned on `cal`; numbers below are `test` only.\n"]
        if "A1" in d:
            a = d["A1"]; L.append("## A1 Parser unit: v1 (original) vs v2 (overlay)\n\n|metric|v1|v2|\n|--|--|--|")
            for k, v in a["rows"].items(): L.append(f"|{k}|{v[0]}|{v[1]}|")
            L.append("\nv1 budget failures by group → " + ", ".join(f"{g}:{n}" for g, n in a["v1_fail_groups"].items()) + "\n")
        if "A2" in d:
            a = d["A2"]
            if a.get("skipped"): L.append(f"## A2 layer 1\n\nSKIPPED: {a['skipped']}\n")
            else:
                L.append(f"## A2 layer 1 coverage (`{a['module']}`)\n\n|group|n|hit|swallowed multi-intent|swallowed safety|\n|--|--|--|--|--|")
                for g, x in a["groups"].items(): L.append(f"|{g}|{x['n']}|{x['hit']}|{x['multi_swallowed']}|{x['safety_swallowed']}|")
                L.append(f"\nroutes seen: {a['routes']}\n")
        if "A3" in d:
            a = d["A3"]; L.append(f"## A3 Kev L1 safety (yes/no)\n\nthreshold chosen on cal (lowest-FPR thr with recall ≥ {A.min_recall_safety}, tie → median): **{a['thr']}**\n\n|setting|recall|FPR|FN|FP|\n|--|--|--|--|--|")
            for k, x in a["rows"].items(): L.append(f"|{k}|{fmt(x['recall'])}|{fmt(x['fpr'])}|{x['fn']}|{x['fp']}|")
            L.append(f"\nemergency sentences missed (test): **{a['emergency_missed']}** · FN list: {a['fn_texts']}\n")
        if "A4" in d:
            a = d["A4"]; L.append("## A4 Kev L2 intents (multi-label)\n\n|variant|micro P|micro R|micro F1|exact-set|\n|--|--|--|--|--|")
            for k, x in a["rows"].items(): L.append(f"|{k}|{fmt(x['p'])}|{fmt(x['r'])}|{fmt(x['f1'])}|{fmt(x['exact'])}|")
            L.append("\nper-label F1 (union): " + ", ".join(f"{k}={fmt(v)}" for k, v in a["per_label_f1"].items()) + f"\n\nthresholds (cal): {a['thr']}\n")
            L.append("exact-set by group (union): " + ", ".join(f"{g}={fmt(v,2)}" for g, v in a["by_group"].items()) + "\n")
        if "A5" in d:
            a = d["A5"]; L.append("## A5 Kev L3 details\n\n|item|v1/Parser only|final policy|\n|--|--|--|")
            for k, v in a["rows"].items(): L.append(f"|{k}|{v[0]}|{v[1]}|")
            L.append("")
        if "A6" in d:
            a = d["A6"]; L.append("## A6 end-to-end + error propagation (test split)\n\n|error|severity|parser-only|final policy|\n|--|--|--|--|")
            for k, v in a["counts"].items(): L.append(f"|{k}|{v['sev']}|{v['parser_only']}|{v['final']}|")
            L.append(f"\nfully-correct sentences: parser-only **{a['full_ok_parser']}** → final **{a['full_ok_final']}** of {a['n']}\n")
            L.append(f"Kev helped (parser wrong → final right): {a['helped']} · Kev hurt (parser right → final wrong): {a['hurt']}\n")
            L.append("by group (fully correct): " + ", ".join(f"{g}={v}" for g, v in a["by_group"].items()) + "\n")
            L.append(f"severe failures total: **{a['severe_final']}** (parser-only: {a['severe_parser']}) · list in `errors.csv`\n")
        if "A7" in d:
            a = d["A7"]; L.append(f"## A7 latency & calls/turn\n\ncalls per turn: Parser 0 + layer-1 0 + Kev **1** (questions per request p50={a['q_p50']})\n\n|VU|n|wall p50|wall p95|kev p50|queue est p50|errors|\n|--|--|--|--|--|--|--|")
            for x in a["rows"]: L.append(f"|{x['level']}|{x['n']}|{fmt(x['wall_ms']['p50'],0)}|{fmt(x['wall_ms']['p95'],0)}|{fmt(x['kev_ms']['p50'],0)}|{fmt(x['queue_est_ms_p50'],0)}|{x['errors']}|")
            L.append("")
        if "A8" in d:
            a = d["A8"]; L.append(f"## A8 walkthrough regression ({a['ok']}/{a['n']} fully correct)\n\n|id|text|mismatches|\n|--|--|--|")
            for x in a["rows"]: L.append(f"|{x['id']}|{x['text']}|{x['diff'] or 'ok'}|")
            L.append("")
        return "\n".join(L) + "\n"
    run.render = render

    def body():
        test = [c for c in cases if c["split"] == "test"]; cal = [c for c in cases if c["split"] == "cal"]

        # ---- A1
        def a1():
            def score(fn, subset):
                n = len(subset); okv = okr = fb = miss = 0; fails = {}
                for c in subset:
                    b = fn(c); g = c["budget"]
                    if g is None:
                        if b: fb += 1
                    else:
                        if b and b["value"] == g["value"]: okv += 1
                        else: miss += 1; fails[c["group"]] = fails.get(c["group"], 0) + 1
                        if b and b["value"] == g["value"] and b["role"] == g["role"]: okr += 1
                return okv, okr, fb, miss, fails
            wb = [c for c in cases if c["budget"]]; nb = [c for c in cases if not c["budget"]]
            s1 = score(lambda c: v1_budget(P1c[c["id"]]), cases); s2 = score(lambda c: parser_only(c, P2c[c["id"]])["budget"], cases)
            tr = [c for c in cases if c["other_numbers"]]
            f1 = lambda fn: sum(1 for c in tr if fn(c) and (not c["budget"] or fn(c)["value"] != c["budget"]["value"]))
            ing_c = [c for c in cases if any(k in c["text"] for k in ("A酸",))]
            ing1 = sum(1 for c in ing_c if "retinol" in (P1c[c["id"]]["ingredients"] or [])); ing2 = sum(1 for c in ing_c if "retinol" in P2c[c["id"]]["ingredients"])
            av = [c for c in cases if c["avoid"]]
            av2 = sum(1 for c in av if set(c["avoid"]) <= set(P2c[c["id"]]["avoid_hint"]))
            rows = {f"budget value correct (n={len(wb)})": (f"{s1[0]}", f"{s2[0]}"), f"budget value+role correct": (f"{s1[1]}", f"{s2[1]}"),
                    f"false budget when none in gold (n={len(nb)})": (f"{s1[2]}", f"{s2[2]}"), f"false budget on capacity/age numbers (n={len(tr)})": (f"{f1(lambda c: v1_budget(P1c[c['id']]))}", f"{f1(lambda c: parser_only(c, P2c[c['id']])['budget'])}"),
                    f"'A酸' ingredient detected (n={len(ing_c)})": (f"{ing1}", f"{ing2}"), f"avoid-ingredient hint (n={len(av)})": ("0 (no field)", f"{av2}"),
                    "route hint changed by v2": ("-", "0 (v2 adds fields only)")}
            return {"rows": rows, "v1_fail_groups": s1[4]}
        run.step("A1", a1)

        # ---- A2
        def a2():
            fn, mod = load_layer1(A.layer1)
            if not fn: return {"skipped": "prod_dialogue_router.py not found next to the bundle (pass --layer1 PATH; read-only import of a COPY of production layer 1)"}
            groups = {}; routes = {}
            for c in cases:
                try: r = fn(c["text"])
                except Exception as e: r = {"route": f"ERR:{type(e).__name__}"}
                g = groups.setdefault(c["group"], {"n": 0, "hit": 0, "multi_swallowed": 0, "safety_swallowed": 0}); g["n"] += 1
                if r: g["hit"] += 1; routes[r.get("route")] = routes.get(r.get("route"), 0) + 1
                if r and len(c["intents"]) > 1: g["multi_swallowed"] += 1
                if r and c["safety"]: g["safety_swallowed"] += 1
            return {"module": mod, "groups": groups, "routes": routes}
        run.step("A2", a2)
        if A.no_kev:
            def a8p():
                rows = []
                for c in [c for c in cases if c["group"] == "regression"]:
                    out = parser_only(c, P2c[c["id"]]); e = [x[0] for x in errors(c, out)]
                    rows.append({"id": c["id"], "text": c["text"], "diff": ", ".join(e)})
                return {"n": len(rows), "ok": sum(1 for x in rows if not x["diff"]), "rows": rows}
            run.step("A8", a8p); return

        # ---- collection (one Kev request per sentence, resumable)
        cl_f = lambda: K.KevClient(A.base_url)
        todo = [c for c in cases if c["id"] not in RAW]
        run.k += 1; G.Bar(run.nsteps, "gateA overall").update(run.k - 1)
        print(f"[gateA step {run.k}/{run.nsteps}] collect Kev answers: {len(RAW)} cached, {len(todo)} to ask", flush=True)
        bar = G.Bar(len(cases), "  collect"); bar.update(len(RAW))
        lk = threading.Lock(); it = iter(todo); fh = open(raw_p, "a"); stop = [False]
        def worker():
            cl = cl_f()
            while not stop[0]:
                with lk:
                    c = next(it, None)
                if c is None: return
                q = K.full_bundle(P2c[c["id"]], P2.ING_ZH); r = cl.ask(c["text"], q)
                rec = {"id": c["id"], "status": r["status"], "wall_ms": r["wall_ms"], "kev_ms": r["kev_ms"], "answers": r["answers"], "nq": len(q)}
                with lk: RAW[c["id"]] = rec; fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush(); bar.update(len(RAW))
        th = [threading.Thread(target=worker, daemon=True) for _ in range(A.vus)]
        [t.start() for t in th]
        try:
            while any(t.is_alive() for t in th): time.sleep(0.5)
        except KeyboardInterrupt:
            stop[0] = True; raise
        fh.close(); bar.done()
        bad = [i for i, r in RAW.items() if not r["answers"]]
        if bad: print(f"  [warn] {len(bad)} sentences got no Kev answer (counted as Kev-unavailable → Parser-only fallback)", flush=True)

        kv = {c["id"]: kev_view(c, RAW.get(c["id"], {}), P2c[c["id"]], None) for c in cases}

        # ---- A3
        def a3():
            def sweep(subset):
                pos = [c for c in subset if c["safety"]]; neg = [c for c in subset if not c["safety"]]
                out = {}
                for t in GRID:
                    fn = sum(1 for c in pos if not ((kv[c["id"]]["p_safety"] or 0) >= t or P2c[c["id"]].get("safety_marker")))
                    fp = sum(1 for c in neg if ((kv[c["id"]]["p_safety"] or 0) >= t or P2c[c["id"]].get("safety_marker")))
                    out[t] = {"recall": (len(pos) - fn) / len(pos) if pos else None, "fpr": fp / len(neg) if neg else None, "fn": fn, "fp": fp}
                return out
            sc = sweep(cal); okt = [t for t in GRID if sc[t]["recall"] is not None and sc[t]["recall"] >= A.min_recall_safety]
            if okt:                                                    # among thresholds meeting the recall floor take the lowest FPR; ties -> median (not an edge of the plateau)
                m = min(sc[t]["fpr"] for t in okt); tied = [t for t in okt if sc[t]["fpr"] <= m + 1e-9]; thr = tied[len(tied) // 2]
            else: thr = max(GRID, key=lambda t: (sc[t]["recall"] or 0, -(sc[t]["fpr"] or 0)))
            st = sweep(test); rows = {f"thr={thr} (chosen) + parser floor": st[thr], "thr=0.5 + parser floor": st[0.5], "thr=0.9 + parser floor": st[0.9]}
            pf = [c for c in test if c["safety"] and not P2c[c["id"]].get("safety_marker")]
            rows["parser flag alone"] = {"recall": 1 - len(pf) / max(1, sum(c['safety'] for c in test)), "fpr": sum(1 for c in test if not c["safety"] and P2c[c["id"]].get("safety_marker")) / max(1, sum(not c['safety'] for c in test)),
                                         "fn": len(pf), "fp": sum(1 for c in test if not c["safety"] and P2c[c["id"]].get("safety_marker"))}
            fnl = [c["text"] for c in test if c["safety"] and not ((kv[c["id"]]["p_safety"] or 0) >= thr or P2c[c["id"]].get("safety_marker"))]
            em = sum(1 for c in test if c["emergency"] and c["text"] in fnl)
            return {"thr": thr, "rows": rows, "fn_texts": fnl[:8], "emergency_missed": em}
        a3r = run.step("A3", a3)

        # ---- A4
        def a4():
            thr = {}
            for l in LABELS:
                fs = []
                for t in GRID:
                    tp = fp = fn = 0
                    for c in cal:
                        pv = kv[c["id"]]["p_intent"].get(l); pred = pv is not None and pv >= t; g = l in c["intents"]
                        tp += pred and g; fp += pred and not g; fn += (not pred) and g
                    fs.append((prf(tp, fp, fn)["f1"] or 0, t))
                top = max(f for f, _ in fs); tied = [t for f, t in fs if f >= top - 1e-9]; thr[l] = tied[len(tied) // 2]
            def evaluate(pred_fn):
                tp = fp = fn = ex = 0; perl = {l: [0, 0, 0] for l in LABELS}; grp = {}
                for c in test:
                    pr = set(pred_fn(c)); g = set(c["intents"]); tp += len(pr & g); fp += len(pr - g); fn += len(g - pr); ok = pr == g; ex += ok
                    x = grp.setdefault(c["group"], [0, 0]); x[0] += ok; x[1] += 1
                    for l in LABELS: perl[l][0] += l in pr and l in g; perl[l][1] += l in pr and l not in g; perl[l][2] += l not in pr and l in g
                o = prf(tp, fp, fn); o["exact"] = ex / len(test); o["perl"] = {l: prf(*v)["f1"] for l, v in perl.items()}; o["grp"] = {g: v[0] / v[1] for g, v in grp.items()}; return o
            kev_only = lambda c: [l for l in LABELS if (kv[c["id"]]["p_intent"].get(l) or 0) >= thr[l]]
            par = lambda c: parser_intents(P2c[c["id"]])
            uni = lambda c: sorted(set(kev_only(c)) | set(par(c)))
            single = lambda c: [kv[c["id"]]["intent_choice"]] if kv[c["id"]]["intent_choice"] else []
            R = {"kev multi-label (tuned thr)": evaluate(kev_only), "parser flags only": evaluate(par), "union kev ∪ parser": evaluate(uni), "kev single-choice argmax (old style)": evaluate(single)}
            u = R["union kev ∪ parser"]
            return {"thr": thr, "rows": {k: {x: v[x] for x in ("p", "r", "f1", "exact")} for k, v in R.items()}, "per_label_f1": u["perl"], "by_group": u["grp"]}
        a4r = run.step("A4", a4)
        th = {"safety": a3r["thr"], "intent": a4r["thr"], "avoid": 0.5}

        # ---- A5
        def a5():
            fi = [c for c in test if "find_product" in c["intents"]]; rows = {}
            con_c = [c for c in fi if c["concern"] or True]
            ok_k = sum(1 for c in con_c if (decide(c, P2c[c["id"]], kv[c["id"]], th)["concern"]) == c["concern"])
            rows[f"concern correct (None counts, n={len(con_c)})"] = ("n/a (v1 has no concern slot)", f"{ok_k}")
            bt = [c for c in test if c["budget"]]; nb = [c for c in test if not c["budget"]]
            v1 = sum(1 for c in bt if (v1_budget(P1c[c['id']]) or {}).get("value") == c["budget"]["value"]); fin = sum(1 for c in bt if (decide(c, P2c[c['id']], kv[c['id']], th)['budget'] or {}).get("value") == c["budget"]["value"])
            rows[f"budget value correct (n={len(bt)})"] = (v1, fin)
            v1r = sum(1 for c in bt if v1_budget(P1c[c['id']]) == c["budget"]); finr = sum(1 for c in bt if (lambda b: b and b["value"] == c["budget"]["value"] and c["budget"]["role"] in ("any", b["role"]))(decide(c, P2c[c['id']], kv[c['id']], th)['budget']))
            rows["budget value+role correct"] = (v1r, finr)
            rows[f"false budget (n={len(nb)} without)"] = (sum(1 for c in nb if v1_budget(P1c[c['id']])), sum(1 for c in nb if decide(c, P2c[c['id']], kv[c['id']], th)['budget']))
            ev = [c for c in test if c["avoid"]]
            rows[f"avoid ingredient found (n={len(ev)})"] = ("0", sum(1 for c in ev if set(c["avoid"]) <= set(decide(c, P2c[c['id']], kv[c['id']], th)['avoid'])))
            unneeded = [c for c in test if not c["avoid"] and P2c[c["id"]]["ingredients"]]
            rows[f"avoid false positive (n={len(unneeded)} mention ingredient w/o avoiding)"] = ("-", sum(1 for c in unneeded if decide(c, P2c[c['id']], kv[c['id']], th)['avoid']))
            rr = [(c, P2c[c["id"]]) for c in test if c["budget"] and any(cd["role"] is None for cd in P2c[c["id"]]["budget_candidates"])]
            rows[f"Kev role needed & right (n={len(rr)})"] = ("-", sum(1 for c, p in rr if (decide(c, p, kv[c['id']], th)['budget'] or {}).get('role') == c['budget']['role']))
            return {"rows": rows}
        run.step("A5", a5)

        # ---- A6
        def a6():
            cnt = {}; full_p = full_f = helped = hurt = sev_p = sev_f = 0; grp = {}; rows = []
            for c in test:
                p2 = P2c[c["id"]]; fo = decide(c, p2, kv[c["id"]], th); po = parser_only(c, p2); ef = errors(c, fo); ep = errors(c, po)
                pc = {e[0] for e in ep}; fc = {e[0] for e in ef}
                for code, sev in ep: cnt.setdefault(code, {"sev": sev, "parser_only": 0, "final": 0})["parser_only"] += 1
                for code, sev in ef: cnt.setdefault(code, {"sev": sev, "parser_only": 0, "final": 0})["final"] += 1
                full_p += not ep; full_f += not ef; sev_p += sum(1 for e in ep if e[1] == "severe"); sev_f += sum(1 for e in ef if e[1] == "severe")
                helped += len(pc - fc); hurt += len(fc - pc)
                g = grp.setdefault(c["group"], [0, 0]); g[0] += not ef; g[1] += 1
                if ef: rows.append((c["id"], c["group"], c["text"], ";".join(f"{a}/{b}" for a, b in ef), json.dumps(fo, ensure_ascii=False)))
            with open(os.path.join(run.dir, "errors.csv"), "w", encoding="utf-8") as f:
                f.write("id\tgroup\ttext\terrors\tdecision\n"); [f.write("\t".join(r) + "\n") for r in rows]
            return {"n": len(test), "counts": dict(sorted(cnt.items(), key=lambda kv_: {"severe": 0, "moderate": 1, "minor": 2}[kv_[1]["sev"]])), "full_ok_parser": full_p, "full_ok_final": full_f,
                    "helped": helped, "hurt": hurt, "by_group": {g: f"{v[0]}/{v[1]}" for g, v in grp.items()}, "severe_parser": sev_p, "severe_final": sev_f}
        run.step("A6", a6)

        # ---- A7
        def a7():
            from gate0_probe import ladder
            sub = cases[:A.latency_n]; qs_list = [K.production_bundle(P2c[c["id"]], P2.ING_ZH, need_concern=("find_product" in c["intents"])) for c in sub]
            texts = [c["text"] for c in sub]; rows = []
            qn = sorted(len(q) for q in qs_list)
            for lv in (1, 4, 8, 16):
                if run.interrupted: break
                idx = [0]; lk2 = threading.Lock(); W, KV, ER = [], [], [0]
                def w():
                    cl = K.KevClient(A.base_url)
                    while True:
                        with lk2:
                            if idx[0] >= max(len(sub), lv * 4): return
                            i = idx[0]; idx[0] += 1
                        r = cl.ask(texts[i % len(sub)], qs_list[i % len(sub)])
                        if r["status"] != 200 or not r["answers"]: ER[0] += 1; continue
                        W.append(r["wall_ms"]); KV.append(r["kev_ms"])
                t0 = time.perf_counter(); ts = [threading.Thread(target=w) for _ in range(lv)]; [t.start() for t in ts]; [t.join() for t in ts]
                ww, kk = G.lat(W), G.lat(KV)
                rows.append({"level": lv, "n": len(W), "errors": ER[0], "wall_ms": ww, "kev_ms": kk, "queue_est_ms_p50": (ww["p50"] - kk["p50"]) if ww["p50"] is not None and kk["p50"] is not None else None})
            return {"rows": rows, "q_p50": qn[len(qn) // 2]}
        run.step("A7", a7)

        # ---- A8
        def a8():
            rows = []
            for c in [c for c in cases if c["group"] == "regression"]:
                fo = decide(c, P2c[c["id"]], kv[c["id"]], th); e = [x[0] for x in errors(c, fo)]
                rows.append({"id": c["id"], "text": c["text"], "diff": ", ".join(e)})
            return {"n": len(rows), "ok": sum(1 for x in rows if not x["diff"]), "rows": rows}
        run.step("A8", a8)
    return G.main_guard(run, body)


if __name__ == "__main__":
    sys.exit(main())
