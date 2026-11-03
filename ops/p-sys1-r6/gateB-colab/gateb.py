#!/usr/bin/env python3
"""Gate B (Colab) — 對話內容解析器模組層比較：Parser+Kev（P+K / Sys1）vs Gemma 4 E2B（純 LLM），只比意圖解析階段，不做 Full path。

Subcommands (all write only under --run DIR; every collect step is append-only jsonl → resumable, partial results analysable):
  env           record environment facts (GPU, versions, model files) into DIR/meta.json
  kev-collect   noul-direction probe + one Kev request per gold sentence (the questions P+K's decision actually reads)
  gemma-collect one llama-server request per sentence per arm  (--quant label must match the GGUF the server has loaded)
  latency       concurrency ladder (VU 1/2/4…) for Kev or a Gemma arm: wall / server-inference / queue-estimate, per VU
  analyze       OFFLINE: thresholds tuned on `cal`, every number reported on `test`; writes SUMMARY.md, metrics.json, per_case.csv, errors/
  status        one-line coverage per arm

Targets are hard-limited: Kev 127.0.0.1:8230-8239 (kevlib), Gemma 127.0.0.1:8350-8359 (llmclient). No other host or port is ever contacted.
Gold = vendor_fk/gate_data.py (seeded synthetic, same generator as Gate A): it measures wiring/robustness, not real-user accuracy.
Colab latency is only meaningful as ratios / token counts on the recorded GPU, never as absolute production numbers.
"""
from __future__ import annotations
import argparse, csv, json, math, os, shutil, signal, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor_fk")); sys.path.insert(0, HERE)
import kevlib as K, gaterun as G, gate_data as D, parser_v2 as P2       # noqa: E402
import gateA as GA                                                       # noqa: E402  (decide / kev_view / errors / parser_only, unchanged)
import gemma_arms as GM                                                  # noqa: E402
import extra_safety as XS                                                # noqa: E402
import holdout as HO                                                     # noqa: E402  (router_gold hold-out, holdout/SCORING.md)


def PR():
    import prod_replay                                                     # production code (vendor_prod/); imported only when prod arms exist
    return prod_replay
from llmclient import LLMClient, check_base as llm_check                 # noqa: E402

LABELS = list(K.INTENT_LABELS)
GRID = GA.GRID
TRUE_S = ["孕婦可以用水楊酸嗎", "A酸和果酸可以一起用嗎", "用了這個會不會過敏", "哺乳期能擦視黃醇嗎", "敏感肌用果酸安全嗎"]
FALSE_S = ["推薦一款保濕乳液", "你好", "幫我記錄今天的膚況", "預算八百想找防曬", "菸鹼醯胺有什麼功效"]
QUANT_NOTE = {"q8_0": "8-bit GGUF Q8_0（PTQ；以 Q8_0 代替 SFP8——SFP8 是 gemma.cpp 格式，llama.cpp 不支援）", "q4_0": "4-bit Q4_0 GGUF"}
SIZES = ("e2b", "e4b", "12b", "26b", "31b")


def label_parts(label):
    """'q4_0' -> ('e2b', 'q4_0'); '12b-q4_0' -> ('12b', 'q4_0'). The label names the result folder gemma/<label>/."""
    l = label.lower(); size = next((z for z in SIZES if l.startswith(z + "-")), None)
    return (size or "e2b"), (l[len(size) + 1:] if size else l)


def label_matches(label, model_path):
    """the loaded GGUF must carry the label's quant AND model size (E2B when the label has no size prefix)."""
    size, quant = label_parts(label); p = (model_path or "").lower().replace("-", "_")
    psize = next((z for z in SIZES if z in p), "e2b")
    return quant in p and psize == size


def label_note(label):
    size, quant = label_parts(label)
    return f"Gemma 4 {size.upper()} " + QUANT_NOTE.get(quant, quant)


def kev_labels(R):
    labs = ["0.8b"] if os.path.exists(R.p("kev", "raw.jsonl")) else []
    kd = os.path.join(R.path, "kev")
    labs += sorted(d for d in (os.listdir(kd) if os.path.isdir(kd) else []) if os.path.exists(os.path.join(kd, d, "raw.jsonl")))
    return labs


def kev_dir(label):
    """Kev results folder: the production candidate (0.8b) keeps the original kev/ path, other Kev sizes get kev/<label>/."""
    return ("kev",) if label in ("0.8b", "kev-0.8b") else ("kev", label)
STOP = [False]


def _sig(*_):
    STOP[0] = True
    raise KeyboardInterrupt


# ============================================================================ run dir
def write_json(path, obj, **kw):
    """Durable JSON write that also works on the Google Drive FUSE mount, where a rename right after writing can fail
    with ENOENT: write tmp (closed + fsync'd), rename with retries, and fall back to writing the target directly."""
    kw.setdefault("ensure_ascii", False); kw.setdefault("indent", 1)
    data = json.dumps(obj, **kw); tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        for i in range(6):
            try:
                os.replace(tmp, path); return
            except FileNotFoundError:
                time.sleep(0.5 * (i + 1))
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    try: os.remove(tmp)
    except OSError: pass


class RunDir:
    def __init__(self, path, seed, n_per, create=True, dataset="gate_data"):
        self.path = path; os.makedirs(path, exist_ok=True)
        self.meta_p = os.path.join(path, "meta.json"); self.dataset = dataset
        m = json.load(open(self.meta_p)) if os.path.exists(self.meta_p) else {}
        if m and m.get("dataset", "gate_data") != dataset:
            sys.exit(f"[gateb] {path} holds dataset {m.get('dataset', 'gate_data')!r}; pass --dataset {m.get('dataset', 'gate_data')} or use a new --run")
        if dataset == "router_gold": seed, n_per = None, None          # the hold-out has fixed cases
        if m and (m.get("seed"), m.get("n_per")) != (seed, n_per):
            sys.exit(f"[gateb] {path} was created with seed={m.get('seed')} n_per={m.get('n_per')}; pass the same values or use a new --run")
        if not m:
            m = {"created": time.strftime("%F %T"), "seed": seed, "n_per": n_per, "catalogue_signature": K.catalogue_signature(),
                 "prompt_signature": GM.prompt_signature(), "env": {}, "llama_models": {}, "dataset": dataset}
            if dataset == "router_gold":
                ok, bad = HO.check_freeze(); m["freeze_at_create"] = {"frozen": ok, "changed": bad}
            self.meta = m; self.save()
        self.meta = m
        if m.get("catalogue_signature") != K.catalogue_signature(): print("[warn] Kev question catalogue changed since this run was created", file=sys.stderr)
        if m.get("prompt_signature") != GM.prompt_signature(): print("[warn] Gemma prompts/schemas changed since this run was created (old jsonl rows used the old prompt)", file=sys.stderr)
        self.cases = HO.cases() if dataset == "router_gold" else D.build(seed, n_per) + XS.cases()   # gate_data: + safety supplement (split "extra")
        write_json(os.path.join(path, "cases.json"), self.cases)
        self.byid = {c["id"]: c for c in self.cases}

    def save(self):
        write_json(self.meta_p, self.meta)

    def status(self, s):
        open(os.path.join(self.path, "STATUS.txt"), "w").write(time.strftime("%F %T ") + s + "\n")

    def p(self, *a):
        q = os.path.join(self.path, *a); os.makedirs(os.path.dirname(q), exist_ok=True); return q


def read_jsonl(p):
    out = {}
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            try: j = json.loads(ln); out[j["id"]] = j
            except Exception: pass                    # torn last line after a kill: that case is re-collected
    return out


def open_append(p):
    """append handle that first terminates a torn last line (left by a kill mid-write) so the next record stays parseable"""
    if os.path.exists(p) and os.path.getsize(p):
        with open(p, "rb") as f:
            f.seek(-1, 2); last = f.read(1)
        if last != b"\n":
            with open(p, "a", encoding="utf-8") as f: f.write("\n")
    return open(p, "a", encoding="utf-8")


def write_jsonl(p, rows):
    tmp = f"{p}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())
    for i in range(6):
        try: os.replace(tmp, p); return
        except FileNotFoundError: time.sleep(0.5 * (i + 1))
    shutil.copy(tmp, p); os.remove(tmp)


def parse_all(cases):
    P2c, ms = {}, {}
    for c in cases:
        t0 = time.perf_counter(); P2c[c["id"]] = P2.parse2(c["text"]); ms[c["id"]] = (time.perf_counter() - t0) * 1000
    return P2c, ms


# v2: an emergency is a REPORT of a reaction, not a question about safety, so the Gate A safety question scores it low
# (first Colab run: 0.24-0.42 on emergencies → the safety threshold had to drop to 0.2 → 80% false alarms). Ask it separately.
EMERGENCY_Q = K.noul("這句話是否描述使用產品後出現的身體不適或可能危急的反應（腫脹、呼吸困難、喘、全身紅疹、頭暈、起水泡）？",
                     "描述了使用後的不適或危急反應", "沒有描述不適反應")


def pk_bundle(p2):
    """The questions gateA.decide() reads (full_bundle minus the unused single-choice intent question) + the v2 emergency question."""
    q = K.full_bundle(p2, P2.ING_ZH); q.pop("intent_choice", None); q["emergency"] = EMERGENCY_Q; return q


def gpu_facts():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,compute_cap,memory.total,driver_version", "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        n, cc, mem, drv = [x.strip() for x in o.split(",")]
        return {"gpu": n, "compute_cap": cc, "gpu_mem": mem, "driver": drv}
    except Exception:
        return {"gpu": None}


# ============================================================================ collect: Kev
def cmd_kev_collect(A, R):
    K.check_base(A.kev_url)
    cl = K.KevClient(A.kev_url, timeout=A.timeout)
    m = cl.models()
    if m["status"] != 200: sys.exit(f"[gateb] Kev not answering on {A.kev_url}: {m['text']}")
    R.meta["env"].setdefault("kev_models", m["json"]); R.meta["env"].update({k: v for k, v in gpu_facts().items()}); R.save()
    KD = kev_dir(A.kev_label); R.meta["env"].setdefault("kev_by_label", {})[A.kev_label] = m["json"]; R.save()
    pp = R.p(*KD, "probe.json")
    if not os.path.exists(pp):
        tv, fv = [], []
        for s, dst in [(s, tv) for s in TRUE_S] + [(s, fv) for s in FALSE_S]:
            r = cl.ask(s, {"safety": K.SAFETY_Q})
            try: dst.append(float(r["answers"]["safety"]["noul"]))
            except Exception: pass
        probe = {"true_mean": sum(tv) / len(tv) if tv else None, "false_mean": sum(fv) / len(fv) if fv else None, "true": tv, "false": fv}
        probe["noul_true_high"] = bool(tv and fv and probe["true_mean"] > probe["false_mean"])
        probe["documented"] = "kev api.py: noul = p('true') → high = yes"
        write_json(pp, probe)
        print(f"[kev] noul probe: mean p on 'is safety' sentences={probe['true_mean']} vs others={probe['false_mean']} → noul_true_high={probe['noul_true_high']}"
              + ("" if probe["noul_true_high"] else "   [WARN] contradicts Kev docs (high=yes) — check the server/model"))
    P2c, pms = parse_all(R.cases)
    rawp = R.p(*KD, "raw.jsonl"); RAW = read_jsonl(rawp)
    have = lambda i: i in RAW and isinstance(RAW[i].get("answers"), dict) and "emergency" in RAW[i]["answers"]   # rows from the v1 bundle are re-asked
    todo = [c for c in R.cases if not have(c["id"]) and (A.split == "all" or c["split"] in (A.split, "cal", "extra", "holdout"))]
    print(f"[kev] {sum(have(i) for i in RAW)} cached, {len(todo)} to ask (cal always collected — P+K thresholds are tuned on it; + safety supplement)", flush=True)
    bar = G.Bar(len(RAW) + len(todo), "kev collect"); bar.update(len(RAW))
    lk = threading.Lock(); it = iter(todo); fh = open_append(rawp)
    def worker():
        c2 = K.KevClient(A.kev_url, timeout=A.timeout)
        while not STOP[0]:
            with lk: c = next(it, None)
            if c is None: return
            q = pk_bundle(P2c[c["id"]]); r = c2.ask(c["text"], q)
            j = r["json"] if isinstance(r["json"], dict) else {}
            rec = {"id": c["id"], "status": r["status"], "wall_ms": r["wall_ms"], "kev_ms": r["kev_ms"], "parser_ms": pms[c["id"]], "nq": len(q),
                   "answers": r["answers"], "usage": j.get("usage"), "err": None if r["status"] == 200 else (r["text"] or str(j)[:200])}
            with lk: RAW[c["id"]] = rec; fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush(); bar.update(len(RAW))
    R.status("RUNNING kev-collect")
    ths = [threading.Thread(target=worker, daemon=True) for _ in range(A.vus)]; [t.start() for t in ths]
    try:
        while any(t.is_alive() for t in ths): time.sleep(0.3)
    finally:
        fh.close(); bar.done()
    bad = sum(1 for r in RAW.values() if not r.get("answers"))
    if bad: print(f"[kev] [warn] {bad} sentences without a Kev answer (P+K falls back to Parser-only for them)")


# ============================================================================ collect: Gemma
def llama_model_info(cl):
    pr = cl.props(); j = pr["json"] if isinstance(pr["json"], dict) else {}
    return {"model_path": j.get("model_path"), "build_info": j.get("build_info"), "n_ctx": (j.get("default_generation_settings") or {}).get("n_ctx"),
            "total_slots": j.get("total_slots"), "chat_template_head": (j.get("chat_template") or "")[:120]}


def cmd_gemma_collect(A, R):
    llm_check(A.llm_url)
    cl = LLMClient(A.llm_url, timeout=A.timeout)
    h = cl.health()
    if h["status"] != 200: sys.exit(f"[gateb] llama-server not healthy on {A.llm_url}: {h['text'] or h['json']}")
    info = llama_model_info(cl); mp = (info.get("model_path") or "").lower()
    if not label_matches(A.quant, mp) and not A.force_quant_label:
        sys.exit(f"[gateb] --quant {A.quant} (= Gemma 4 {label_parts(A.quant)[0].upper()} {label_parts(A.quant)[1]}) but the server has loaded {info.get('model_path')!r}; pass the matching label, e.g. 12b-q4_0")
    R.meta["llama_models"][A.quant] = info; R.meta["env"].update(gpu_facts()); R.save()
    arms = [a.strip() for a in A.arms.split(",") if a.strip()]
    [GM.arm_parts(a) for a in arms]
    cases = [c for c in R.cases if A.split == "all" or c["split"] in (A.split, "holdout") or (c["split"] == "extra" and not A.no_extra)]
    if A.limit: cases = cases[:A.limit]
    llm_path = None
    if R.dataset == "router_gold":                       # SCORING.md §3: prod arms only for sentences the production replay sends to the LLM
        llm_path = {c["id"] for c in cases if PR().deterministic(c["text"])[1] is None} if any(GM.arm_parts(a)[0] == "prod" for a in arms) else set()
    # warm-up (not recorded): first request pays CUDA graph / kernel compile costs
    cl.chat(GM.request_body(arms[0], "你好", 16))
    for ai, arm in enumerate(arms):
        if STOP[0]: break
        p = R.p("gemma", A.quant, f"{arm}.jsonl"); RAW = read_jsonl(p)
        leaked = [i for i, r in RAW.items() if r.get("reasoning")]
        if leaked and A.redo_thinking:                     # rows where thinking leaked (first run, json arms): archive once, then re-collect
            bak = p + ".thinking-leak.bak"
            if not os.path.exists(bak): shutil.copy(p, bak)
            for i in leaked: RAW.pop(i)
            write_jsonl(p, RAW.values())
            print(f"[gemma {A.quant}] {arm}: {len(leaked)} rows had leaked thinking → re-collect (old rows kept in {os.path.basename(bak)})", flush=True)
        elif leaked:
            print(f"[gemma {A.quant}] [warn] {arm}: {len(leaked)} rows contain thinking output; pass --redo-thinking to re-collect them", flush=True)
        todo = [c for c in cases if c["id"] not in RAW and not (llm_path is not None and GM.arm_parts(arm)[0] == "prod" and c["id"] not in llm_path)]
        print(f"[gemma {A.quant}] arm {ai + 1}/{len(arms)} {arm}: {len(RAW)} cached, {len(todo)} to ask", flush=True)
        if not todo: continue
        bar = G.Bar(len(RAW) + len(todo), f"  {arm}"); bar.update(len(RAW))
        R.status(f"RUNNING gemma-collect {A.quant} {arm}")
        with open_append(p) as fh:
            for c in todo:
                if STOP[0]: break
                r = cl.chat(GM.request_body(arm, c["text"], A.max_tokens, A.seed)); t = r["timings"] or {}; u = r["usage"] or {}
                rec = {"id": c["id"], "status": r["status"], "wall_ms": r["wall_ms"], "server_ms": r["server_ms"], "queue_est_ms": r["queue_est_ms"],
                       "prompt_ms": t.get("prompt_ms"), "predicted_ms": t.get("predicted_ms"), "prompt_n": t.get("prompt_n", u.get("prompt_tokens")),
                       "cache_n": t.get("cache_n"), "predicted_n": t.get("predicted_n", u.get("completion_tokens")), "finish": r["finish"],
                       "content": r["content"], "reasoning": r["reasoning"], "error": r["error"]}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n"); fh.flush(); RAW[c["id"]] = rec; bar.update(len(RAW))
        bar.done()


# ============================================================================ latency ladder
def cmd_latency(A, R):
    levels = [int(x) for x in A.levels.split(",")]
    test = [c for c in R.cases if c["split"] == "test"][:A.n]
    P2c, pms = parse_all(test)
    if A.target == "kev":
        K.check_base(A.kev_url); tag = "kev-pk" if kev_dir(A.kev_label) == ("kev",) else f"kev-{A.kev_label}-pk"
        mk = lambda: K.KevClient(A.kev_url, timeout=A.timeout)
        def call(cl, c):
            r = cl.ask(c["text"], pk_bundle(P2c[c["id"]]))
            ok = r["status"] == 200 and bool(r["answers"])
            return ok, {"wall_ms": r["wall_ms"] + pms[c["id"]], "server_ms": r["kev_ms"], "queue_est_ms": (r["wall_ms"] - r["kev_ms"]) if r["kev_ms"] is not None else None, "out_tok": 0}
        arms = [None]
    else:
        llm_check(A.llm_url); arms = [a.strip() for a in A.arms.split(",") if a.strip()]
        mk = lambda: LLMClient(A.llm_url, timeout=A.timeout)
        info = llama_model_info(LLMClient(A.llm_url))
        if not label_matches(A.quant, info.get("model_path")) and not A.force_quant_label:
            sys.exit(f"[gateb] --quant {A.quant} but the server has loaded {info.get('model_path')!r}")
        slots = info.get("total_slots") or 1
        if max(levels) > slots: print(f"[warn] server has {slots} slots; VU>{slots} measures queueing inside llama-server", file=sys.stderr)
    for arm in arms:
        if A.target == "gemma":
            tag = f"gemma-{A.quant}-{arm}"
            def call(cl, c, arm=arm):
                r = cl.chat(GM.request_body(arm, c["text"], A.max_tokens, A.seed)); t = r["timings"] or {}
                return r["status"] == 200, {"wall_ms": r["wall_ms"], "server_ms": r["server_ms"], "queue_est_ms": r["queue_est_ms"], "out_tok": t.get("predicted_n")}
        outp = R.p("latency", f"{tag}.json"); res = json.load(open(outp)) if os.path.exists(outp) else {"tag": tag, "levels": {}, "gpu": gpu_facts()}
        for lv in levels:
            if STOP[0]: break
            if str(lv) in res["levels"] and not A.redo: print(f"[latency] {tag} VU={lv} reuse"); continue
            total = max(len(test), lv * 8); idx = [0]; lk = threading.Lock(); S = []; per = {}; err = [0]
            bar = G.Bar(total, f"[latency] {tag} VU={lv}")
            def worker(vu):
                cl = mk(); mine = []
                while not STOP[0]:
                    with lk:
                        if idx[0] >= total: break
                        i = idx[0]; idx[0] += 1
                    ok, x = call(cl, test[i % len(test)])
                    with lk:
                        if ok: S.append(x); mine.append(x["wall_ms"])
                        else: err[0] += 1
                        bar.update(len(S) + err[0])
                per[vu] = G.lat(mine)
            t0 = time.perf_counter(); ts = [threading.Thread(target=worker, args=(v,), daemon=True) for v in range(lv)]
            with G.GpuWatch() as gw:
                [t.start() for t in ts]
                try:
                    while any(t.is_alive() for t in ts): time.sleep(0.2)
                except KeyboardInterrupt:
                    STOP[0] = True; raise
            el = time.perf_counter() - t0; bar.done()
            res["levels"][str(lv)] = {"n": len(S), "errors": err[0], "rps": round(len(S) / el, 3) if el else None, "wall_ms": G.lat([s["wall_ms"] for s in S]),
                                      "server_ms": G.lat([s["server_ms"] for s in S]), "queue_est_ms": G.lat([s["queue_est_ms"] for s in S]),
                                      "out_tok": G.lat([s["out_tok"] for s in S]), "per_vu_wall_p50": {v: x["p50"] for v, x in sorted(per.items())}, "gpu": gw.result()}
            write_json(outp, res)


# ============================================================================ analysis (offline)
def wilson(k, n, z=1.96):
    if not n: return (None, None)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def mcnemar_p(b, c):
    n = b + c
    if n == 0: return 1.0
    k = min(b, c); s = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * s)


def tune_thresholds(cal, kv, P2c, min_recall):
    def safe_pred(c, t): return (kv[c["id"]]["p_safety"] or 0) >= t or bool(P2c[c["id"]].get("safety_marker"))
    pos = [c for c in cal if c["safety"]]; neg = [c for c in cal if not c["safety"]]; sc = {}
    for t in GRID:
        fn = sum(1 for c in pos if not safe_pred(c, t)); fp = sum(1 for c in neg if safe_pred(c, t))
        sc[t] = ((len(pos) - fn) / len(pos) if pos else None, fp / len(neg) if neg else None)
    okt = [t for t in GRID if sc[t][0] is not None and sc[t][0] >= min_recall]
    if okt:
        m = min(sc[t][1] for t in okt); tied = [t for t in okt if sc[t][1] <= m + 1e-9]; ts = tied[len(tied) // 2]
    else:
        ts = max(GRID, key=lambda t: (sc[t][0] or 0, -(sc[t][1] or 0)))
    thr = {}
    for l in LABELS:
        fs = []
        for t in GRID:
            tp = fp = fn = 0
            for c in cal:
                pv = kv[c["id"]]["p_intent"].get(l); pred = pv is not None and pv >= t; g = l in c["intents"]
                tp += pred and g; fp += pred and not g; fn += (not pred) and g
            fs.append((GA.prf(tp, fp, fn)["f1"] or 0, t))
        top = max(f for f, _ in fs); tied = [t for f, t in fs if f >= top - 1e-9]; thr[l] = tied[len(tied) // 2]
    return {"safety": ts, "intent": thr, "avoid": 0.5, "cal_n": len(cal), "cal_safety_pos": len(pos)}


def _tune_noul(pos, neg, pfun, min_recall):
    sc = {}
    for t in GRID:
        fn = sum(1 for c in pos if not pfun(c, t)); fp = sum(1 for c in neg if pfun(c, t))
        sc[t] = ((len(pos) - fn) / len(pos) if pos else None, fp / len(neg) if neg else None)
    okt = [t for t in GRID if sc[t][0] is not None and sc[t][0] >= min_recall]
    if okt:
        m = min(sc[t][1] for t in okt); tied = [t for t in okt if sc[t][1] <= m + 1e-9]; return tied[len(tied) // 2]
    return max(GRID, key=lambda t: (sc[t][0] or 0, -(sc[t][1] or 0)))


def tune_v2(cal, kv, pe, P2c, min_recall):
    """safety threshold tuned on non-emergency safety sentences; emergency threshold tuned on emergency vs non-safety (cal only)."""
    base = tune_thresholds([c for c in cal if not c["emergency"]], kv, P2c, min_recall)
    em_pos = [c for c in cal if c["emergency"]]; em_neg = [c for c in cal if not c["safety"]]
    te = _tune_noul(em_pos, em_neg, lambda c, t: (pe.get(c["id"]) or 0) >= t, min_recall)
    fpr = sum(1 for c in em_neg if (pe.get(c["id"]) or 0) >= te) / max(1, len(em_neg))   # tuned on only a handful of emergencies: watch this
    return {"base": base, "safety": base["safety"], "emergency": te, "cal_emergency_pos": len(em_pos), "emergency_cal_fpr": round(fpr, 3),
            "intent": base["intent"], "unclear": "derived"}


def decide_v2(c, p2, kvv, p_em, th2):
    d = GA.decide(c, p2, kvv, th2["base"])
    em = p_em is not None and p_em >= th2["emergency"]
    d["emergency"] = em; d["safety"] = bool(d["safety"] or em)
    ints = [i for i in d["intents"] if i != "unclear"]             # Kev's unclear question has no signal (AUC≈0.3): unclear = nothing else
    d["intents"] = sorted(ints) if ints else ["unclear"]
    return d


def score_extra(extra, xdec):
    out = {}
    for name, dec in xdec.items():
        E = [c for c in extra if c["id"] in dec]
        if not E: continue
        g = lambda grp: [c for c in E if c["group"] == grp]
        em, ask, neg = g("extra_emergency"), g("extra_ask"), g("extra_neg")
        has_em = any("emergency" in dec[c["id"]] for c in E)
        out[name] = {"n": len(E), "emergency_safety": [sum(dec[c["id"]]["safety"] for c in em), len(em)],
                     "emergency_flag": [sum(bool(dec[c["id"]].get("emergency")) for c in em), len(em)] if has_em else None,
                     "ask_safety": [sum(dec[c["id"]]["safety"] for c in ask), len(ask)], "neg_false": [sum(dec[c["id"]]["safety"] for c in neg), len(neg)],
                     "missed": [c["text"] for c in em + ask if not dec[c["id"]]["safety"]], "false_alarms": [c["text"] for c in neg if dec[c["id"]]["safety"]]}
    return out


def score(name, test, dec, extra=None):
    """dec: id -> decision (gateA.errors format). Cases absent from dec are skipped (coverage reported)."""
    T = [c for c in test if c["id"] in dec]; n = len(T); r = {"name": name, "n": n, "n_test": len(test)}
    pos = [c for c in T if c["safety"]]; neg = [c for c in T if not c["safety"]]
    fn = [c for c in pos if not dec[c["id"]]["safety"]]; fp = [c for c in neg if dec[c["id"]]["safety"]]
    r["safety"] = {"recall": (len(pos) - len(fn)) / len(pos) if pos else None, "recall_ci": wilson(len(pos) - len(fn), len(pos)), "fn": len(fn), "pos": len(pos),
                   "fpr": len(fp) / len(neg) if neg else None, "fp": len(fp), "neg": len(neg), "emergency_missed": sum(1 for c in fn if c["emergency"]),
                   "emergency_n": sum(1 for c in T if c["emergency"]), "fn_texts": [c["text"] for c in fn][:10]}
    tp = fpn = fnn = ex = 0; perl = {l: [0, 0, 0] for l in LABELS}
    for c in T:
        pr, g = set(dec[c["id"]]["intents"]), set(c["intents"]); tp += len(pr & g); fpn += len(pr - g); fnn += len(g - pr); ex += pr == g
        for l in LABELS: perl[l][0] += l in pr and l in g; perl[l][1] += l in pr and l not in g; perl[l][2] += l not in pr and l in g
    r["intent"] = dict(GA.prf(tp, fpn, fnn), exact=ex / n if n else None, exact_ci=wilson(ex, n), per_label_f1={l: GA.prf(*v)["f1"] for l, v in perl.items()})
    bt = [c for c in T if c["budget"]]; nb = [c for c in T if not c["budget"]]
    val = sum(1 for c in bt if (dec[c["id"]]["budget"] or {}).get("value") == c["budget"]["value"])
    vr = sum(1 for c in bt if (lambda b: b and b["value"] == c["budget"]["value"] and c["budget"]["role"] in ("any", b["role"]))(dec[c["id"]]["budget"]))
    r["budget"] = {"n": len(bt), "value_ok": val, "value_role_ok": vr, "false": sum(1 for c in nb if dec[c["id"]]["budget"]), "n_without": len(nb)}
    av = [c for c in T if c["avoid"]]; men = [c for c in T if not c["avoid"] and P2.ingredients_fixed(c["text"])]
    r["avoid"] = {"n": len(av), "found": sum(1 for c in av if set(c["avoid"]) <= set(dec[c["id"]]["avoid"])), "fp_n": len(men), "fp": sum(1 for c in men if dec[c["id"]]["avoid"])}
    fi = [c for c in T if "find_product" in c["intents"]]
    r["concern"] = {"n": len(fi), "ok": sum(1 for c in fi if dec[c["id"]].get("concern") == c["concern"])}
    cnt = {}; sev_cases = sev = full = 0; grp = {}; per = {}
    for c in T:
        e = GA.errors(c, dec[c["id"]]); per[c["id"]] = e
        for code, s in e: cnt.setdefault(code, {"sev": s, "n": 0})["n"] += 1
        ns = sum(1 for _, s in e if s == "severe"); sev += ns; sev_cases += ns > 0; full += not e
        g = grp.setdefault(c["group"], [0, 0]); g[0] += not e; g[1] += 1
    r["errors"] = cnt; r["severe"] = sev; r["severe_cases"] = sev_cases; r["full_ok"] = full; r["full_ok_ci"] = wilson(full, n)
    r["by_group"] = {g: f"{v[0]}/{v[1]}" for g, v in sorted(grp.items())}; r["_per_case"] = per
    if extra: r.update(extra)
    return r


def cmd_analyze(A, R):
    if R.dataset == "router_gold": return cmd_analyze_holdout(A, R)
    test = [c for c in R.cases if c["split"] == "test"]; cal = [c for c in R.cases if c["split"] == "cal"]
    extra = [c for c in R.cases if c["split"] == "extra"]; prod_raws = {}
    P2c, pms = parse_all(R.cases); arms = []; thr = None; thr2 = None; probe = None; xdec = {}; thr_by = {}
    # ---- Parser only
    arms.append(score("Parser only（無模型）", test, {c["id"]: GA.parser_only(c, P2c[c["id"]]) for c in test},
                      {"kind": "parser", "lat": {"total_ms": G.lat([pms[c["id"]] for c in test])}}))
    # ---- P+K (one pair of arms per Kev model; 0.8b = production candidate, its v2 is the reference for ratios / pairing)
    thr_by = {}
    for lab in kev_labels(R):
        KD = kev_dir(lab); kraw = read_jsonl(R.p(*KD, "raw.jsonl")); pp = R.p(*KD, "probe.json")
        if not kraw: continue
        if os.path.exists(pp):
            pr = json.load(open(pp)); K.NOUL_TRUE_HIGH = bool(pr.get("noul_true_high", True))
            if KD == ("kev",): probe = pr
        sfx = "" if KD == ("kev",) else f"・Kev-{lab.upper().removeprefix('KEV-')}"
        kv = {c["id"]: GA.kev_view(c, kraw.get(c["id"], {}), P2c[c["id"]], None) for c in R.cases}
        calk = [c for c in cal if c["id"] in kraw and kraw[c["id"]].get("answers")]
        t1 = tune_thresholds(calk, kv, P2c, A.min_recall_safety)
        dec = {c["id"]: GA.decide(c, P2c[c["id"]], kv[c["id"]], t1) for c in test if c["id"] in kraw}
        tk = [kraw[c["id"]] for c in test if c["id"] in kraw and kraw[c["id"]].get("answers")]
        lat = {"total_ms": G.lat([x["wall_ms"] + x["parser_ms"] for x in tk]), "wall_ms": G.lat([x["wall_ms"] for x in tk]), "server_ms": G.lat([x["kev_ms"] for x in tk]),
               "queue_est_ms": G.lat([(x["wall_ms"] - x["kev_ms"]) if x.get("kev_ms") is not None else None for x in tk]), "parser_ms": G.lat([x["parser_ms"] for x in tk]),
               "nq": G.lat([x["nq"] for x in tk])}
        pk_extra = {"kind": "pk", "kev": lab, "lat": lat, "kev_failed": sum(1 for c in test if c["id"] in kraw and not kraw[c["id"]].get("answers")),
                    "out_tok": {"p50": 0, "mean": 0, "note": "Kev 不生成 token（prefill-only）"}}
        n1 = f"P+K v1（Gate A 規則{sfx}）"
        arms.append(score(n1, test, dec, dict(pk_extra, pk_version=1)))
        xdec[n1] = {c["id"]: GA.decide(c, P2c[c["id"]], kv[c["id"]], t1) for c in extra if c["id"] in kraw}
        t2 = None
        if all(isinstance((kraw.get(c["id"]) or {}).get("answers"), dict) and "emergency" in kraw[c["id"]]["answers"] for c in cal + test):
            pe = {i: K.p_noul(kraw[i]["answers"], "emergency") for i in kraw if isinstance(kraw[i].get("answers"), dict)}
            t2 = tune_v2(cal, kv, pe, P2c, A.min_recall_safety)
            n2 = f"P+K v2（＋急症題、unclear 推導{sfx}）"
            dec2 = {c["id"]: decide_v2(c, P2c[c["id"]], kv[c["id"]], pe.get(c["id"]), t2) for c in test if c["id"] in kraw}
            arms.append(score(n2, test, dec2, dict(pk_extra, pk_version=2)))
            xdec[n2] = {c["id"]: decide_v2(c, P2c[c["id"]], kv[c["id"]], pe.get(c["id"]), t2) for c in extra if c["id"] in kraw}
        else:
            print(f"[analyze] Kev {lab}: rows lack the v2 emergency answer → P+K v2 skipped (rerun kev-collect)", file=sys.stderr)
        thr_by[lab] = {"v1": t1, "v2": t2}
        if KD == ("kev",): thr, thr2 = t1, t2
    # ---- Gemma
    gdir = R.p("gemma", "x"); gdir = os.path.dirname(gdir)
    for q in sorted(os.listdir(gdir)) if os.path.isdir(gdir) else []:
        order = {a: i for i, a in enumerate(GM.DEFAULT_ARMS + GM.PROD_ARMS)}
        for f in sorted(os.listdir(os.path.join(gdir, q)), key=lambda f: order.get(f[:-6], 99)):
            if not f.endswith(".jsonl"): continue
            arm = f[:-6]
            try: kind, fs, mode = GM.arm_parts(arm)
            except SystemExit: continue
            raw = read_jsonl(os.path.join(gdir, q, f)); dec = {}; pf = {}; sv = 0; norm = drop = 0; trunc = 0; reas = 0; httperr = 0
            def decide_row(c, x):
                if kind == "prod":                                     # production post-processing (guards, sanitising, dispatch safety rule)
                    d, err = PR().llm_decision(x.get("content"), c["text"]); return d, {"normalised": [], "dropped": []}, err
                obj, err = GM.extract_json(x.get("content")); d, notes = GM.to_decision(kind, obj); return d, notes, err
            xdec[f"Gemma {q} {arm}"] = {c["id"]: decide_row(c, raw[c["id"]])[0] for c in extra if c["id"] in raw}
            if kind == "prod": prod_raws[(q, arm)] = raw
            for c in test:
                x = raw.get(c["id"])
                if not x: continue
                if x["status"] != 200: httperr += 1
                obj, jerr = GM.extract_json(x.get("content"))
                d, notes, err = decide_row(c, x); err = err or jerr
                if err: pf[err] = pf.get(err, 0) + 1
                elif GM.validate(kind, obj): sv += 1
                dec[c["id"]] = d; norm += len(notes["normalised"]); drop += len(notes["dropped"])
                trunc += x.get("finish") == "length"; reas += bool(x.get("reasoning"))
            rows = [raw[c["id"]] for c in test if c["id"] in raw and raw[c["id"]]["status"] == 200]
            lat = {"total_ms": G.lat([x["wall_ms"] for x in rows]), "wall_ms": G.lat([x["wall_ms"] for x in rows]), "server_ms": G.lat([x["server_ms"] for x in rows]),
                   "queue_est_ms": G.lat([x["queue_est_ms"] for x in rows]), "prompt_ms": G.lat([x["prompt_ms"] for x in rows]), "predicted_ms": G.lat([x["predicted_ms"] for x in rows]),
                   "prompt_n": G.lat([x["prompt_n"] for x in rows]), "cache_n": G.lat([x["cache_n"] for x in rows]),
                   "tok_s": G.lat([(x["predicted_n"] / x["predicted_ms"] * 1000) if x.get("predicted_n") and x.get("predicted_ms") else None for x in rows])}
            nn = len(dec)
            arms.append(score(f"Gemma {q} {arm}", test, dec, {"kind": "gemma", "quant": q, "arm": arm, "schema": kind, "fewshot": fs, "mode": mode, "lat": lat,
                                                              "out_tok": G.lat([x["predicted_n"] for x in rows]), "parse_fail": sum(pf.values()), "parse_fail_kinds": pf,
                                                              "parse_fail_ci": wilson(sum(pf.values()), nn), "schema_violations": sv, "truncated": trunc, "reasoning_seen": reas,
                                                              "http_errors": httperr, "normalised": norm, "dropped": drop}))
    # ---- production path: deterministic layers first, the LLM only for what they leave (prod_replay.py)
    prod_paths = {}
    for (q, arm), raw in sorted(prod_raws.items()):
        name = f"Production 現行路徑（決定性層＋Gemma {q} {arm}）"; dec = {}; lat = []; paths = {}; xd = {}
        for c in test + extra:
            path, d, ms = PR().deterministic(c["text"])
            if d is None:
                x = raw.get(c["id"])
                if not x: continue
                d = PR().llm_decision(x.get("content"), c["text"])[0]; ms = x["wall_ms"]
            if c["split"] == "test":
                dec[c["id"]] = d; lat.append(ms); k = path.split(":")[0] if path.startswith("resolver") else path; paths[k] = paths.get(k, 0) + 1
            else: xd[c["id"]] = d
        xdec[name] = xd; prod_paths[name] = paths
        arms.append(score(name, test, dec, {"kind": "prod_path", "quant": q, "arm": arm, "lat": {"total_ms": G.lat(lat), "mean_ms": sum(lat) / len(lat) if lat else None},
                                            "paths": paths, "llm_share": paths.get("LLM", 0) / max(1, sum(paths.values()))}))
    # ---- paired vs P+K
    pks = [a for a in arms if a.get("kind") == "pk"]
    pk = next((a for a in reversed(pks) if kev_dir(a["kev"]) == ("kev",)), pks[-1] if pks else None)   # Kev-0.8B, newest policy (v2 when available)
    for a in arms:
        if not pk or a is pk: continue
        common = [i for i in a["_per_case"] if i in pk["_per_case"]]
        b = sum(1 for i in common if not pk["_per_case"][i] and a["_per_case"][i]); c_ = sum(1 for i in common if pk["_per_case"][i] and not a["_per_case"][i])
        bs = sum(1 for i in common if not any(s == "severe" for _, s in pk["_per_case"][i]) and any(s == "severe" for _, s in a["_per_case"][i]))
        cs = sum(1 for i in common if any(s == "severe" for _, s in pk["_per_case"][i]) and not any(s == "severe" for _, s in a["_per_case"][i]))
        a["paired_vs_pk"] = {"n": len(common), "pk_ok_arm_wrong": b, "arm_ok_pk_wrong": c_, "p": mcnemar_p(b, c_), "pk_clean_arm_severe": bs, "arm_clean_pk_severe": cs, "p_severe": mcnemar_p(bs, cs)}
    base = (pk or {}).get("lat", {}).get("total_ms", {}).get("p50")
    for a in arms:
        p50 = a["lat"].get("mean_ms") if a.get("kind") == "prod_path" else a.get("lat", {}).get("total_ms", {}).get("p50")   # mixed path: mean is the honest summary
        a["lat_ratio_vs_pk"] = (p50 / base) if (base and p50 is not None and a.get("kind") != "parser") else None
    # ---- latency ladders
    lad = {}
    ld = os.path.dirname(R.p("latency", "x"))
    for f in sorted(os.listdir(ld)) if os.path.isdir(ld) else []:
        if f.endswith(".json"): lad[f[:-5]] = json.load(open(os.path.join(ld, f)))
    # ---- outputs
    os.makedirs(R.p("errors", "x").rsplit("/", 1)[0], exist_ok=True)
    for a in arms:
        safe = a["name"].replace(" ", "_").replace("/", "-").replace("（", "_").replace("）", "").replace("，", "_").replace("→", "-")
        with open(R.p("errors", f"{safe}.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\tgroup\ttext\terrors\n")
            for i, e in a["_per_case"].items():
                if e: fh.write(f"{i}\t{R.byid[i]['group']}\t{R.byid[i]['text']}\t{';'.join(f'{x}/{s}' for x, s in e)}\n")
    with open(R.p("per_case.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh); w.writerow(["id", "group", "text", "gold"] + [a["name"] for a in arms])
        for c in test:
            gold = json.dumps({k: c[k] for k in ("safety", "intents", "budget", "avoid", "concern")}, ensure_ascii=False)
            w.writerow([c["id"], c["group"], c["text"], gold] + ["" if c["id"] not in a["_per_case"] else ("OK" if not a["_per_case"][c["id"]] else ";".join(x for x, _ in a["_per_case"][c["id"]])) for a in arms])
    clean = [{k: v for k, v in a.items() if k != "_per_case"} for a in arms]
    xs = score_extra(extra, xdec)
    write_json(R.p("metrics.json"), {"meta": R.meta, "thresholds": thr, "thresholds_v2": thr2, "thresholds_by_kev": thr_by, "noul_probe": probe, "arms": clean, "latency_ladders": lad, "safety_supplement": xs}, default=str)
    with open(R.p("SUMMARY.md"), "w", encoding="utf-8") as fh: fh.write(render(R, clean, thr, probe, lad, test, thr2, xs))
    print(f"[analyze] {len(arms)} arms → {R.p('SUMMARY.md')}")


HOLDOUT_ARM_NAMES = {"q4_0": "Gemma E2B Q4", "q8_0": "Gemma E2B Q8", "12b-q4_0": "Gemma 12B Q4"}


def cmd_analyze_holdout(A, R):
    """router_gold hold-out: frozen thresholds, fixed route mapping, metrics M1-M7 (holdout/SCORING.md). Nothing is tuned here."""
    frozen, bad = HO.check_freeze()
    FT = json.load(open(os.path.join(HO.HD, "frozen_thresholds.json"), encoding="utf-8"))["by_kev"]
    C = R.cases; P2c, pms = parse_all(C); arms = []; notes = []
    arms.append(HO.score("Parser only（無模型）", C, {c["id"]: GA.parser_only(c, P2c[c["id"]]) for c in C},
                         {"M7_latency": {"kind": "p50", "p50": G.lat([pms[c["id"]] for c in C]).get("p50")}}))
    for lab in kev_labels(R):
        kraw = read_jsonl(R.p(*kev_dir(lab), "raw.jsonl")); pp = R.p(*kev_dir(lab), "probe.json")
        if lab not in FT: notes.append(f"Kev {lab}：沒有凍結門檻，未評分"); continue
        if os.path.exists(pp): K.NOUL_TRUE_HIGH = bool(json.load(open(pp)).get("noul_true_high", True))
        ok = [c for c in C if isinstance((kraw.get(c["id"]) or {}).get("answers"), dict)]
        if not ok: continue
        kv = {c["id"]: GA.kev_view(c, kraw[c["id"]], P2c[c["id"]], None) for c in ok}
        lat = {"kind": "p50", "p50": G.lat([kraw[c["id"]]["wall_ms"] + kraw[c["id"]]["parser_ms"] for c in ok]).get("p50")}
        sfx = f"Kev-{lab.upper()}"
        t1 = FT[lab]["v1"]; t2 = FT[lab]["v2"]
        if lab == "0.8b":
            arms.append(HO.score(f"P+K v1・{sfx}", C, {c["id"]: GA.decide(c, P2c[c["id"]], kv[c["id"]], t1) for c in ok}, {"M7_latency": lat}))
        if t2 and all("emergency" in kraw[c["id"]]["answers"] for c in ok):
            pe = {c["id"]: K.p_noul(kraw[c["id"]]["answers"], "emergency") for c in ok}
            arms.append(HO.score(f"P+K v2・{sfx}", C, {c["id"]: decide_v2(c, P2c[c["id"]], kv[c["id"]], pe[c["id"]], t2) for c in ok}, {"M7_latency": lat, "base": lab == "0.8b"}))
        if len(ok) < len(C): notes.append(f"Kev {lab}：{len(C) - len(ok)} 句沒有 Kev 回答，未計入")
    gdir = os.path.dirname(R.p("gemma", "x"))
    for q in [q for q in HO.GEMMA_ARMS if os.path.isdir(os.path.join(gdir, q))]:
        for arm in HO.GEMMA_ARMS[q]:
            p = os.path.join(gdir, q, f"{arm}.jsonl")
            if not os.path.exists(p): notes.append(f"{q} {arm}：沒有收集"); continue
            raw = read_jsonl(p); kind = GM.arm_parts(arm)[0]
            if kind == "prod":                                   # production path: replayed rules, LLM only where the rules leave the sentence
                dec, ms, paths, miss = {}, [], {}, 0
                for c in C:
                    path, d, t = PR().deterministic(c["text"])
                    if d is None:
                        x = raw.get(c["id"])
                        if not x: miss += 1; continue
                        d = PR().llm_decision(x.get("content"), c["text"])[0]; t = x["wall_ms"]
                    dec[c["id"]] = d; ms.append(t); k = path.split(":")[0] if path.startswith("resolver") else path; paths[k] = paths.get(k, 0) + 1
                if miss: notes.append(f"{q} {arm}：{miss} 句送 LLM 但沒有收集")
                arms.append(HO.score(f"Production 現行路徑（規則層＋{HOLDOUT_ARM_NAMES[q]} prod_json）", C, dec,
                                     {"M7_latency": {"kind": "mean", "mean": sum(ms) / len(ms) if ms else 0}, "paths": paths}))
            else:
                dec = {}; pf = 0
                for c in C:
                    x = raw.get(c["id"])
                    if not x: continue
                    obj, err = GM.extract_json(x.get("content")); pf += bool(err); dec[c["id"]] = GM.to_decision(kind, obj)[0]
                rows = [raw[c["id"]] for c in C if c["id"] in raw and raw[c["id"]]["status"] == 200]
                arms.append(HO.score(f"{HOLDOUT_ARM_NAMES[q]} {arm}", C, dec, {"M7_latency": {"kind": "p50", "p50": G.lat([x["wall_ms"] for x in rows]).get("p50")}, "parse_fail": pf}))
        extra_arms = sorted(f[:-6] for f in os.listdir(os.path.join(gdir, q)) if f.endswith(".jsonl") and f[:-6] not in HO.GEMMA_ARMS[q])
        if extra_arms: notes.append(f"{q}：{', '.join(extra_arms)} 不在事前登錄的臂裡，未評分")
    base = next((a for a in arms if a.get("base")), None)
    for a in arms:
        if base and a is not base: a["paired"] = HO.paired(base, a, C)
    md = HO.render(arms, frozen, bad, R.meta, base["name"] if base else "（缺 P+K v2・Kev-0.8B）")
    if notes: md += "\n## 備註\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    with open(R.p("HOLDOUT_SUMMARY.md"), "w", encoding="utf-8") as fh: fh.write(md)
    with open(R.p("holdout_per_case.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh); w.writerow(["id", "route", "boundary", "source", "text"] + [a["name"] for a in arms])
        for c in C: w.writerow([c["id"], c["route"], int(c["boundary"]), c["source"], c["text"]] + [a["_pred"].get(c["id"], "") for a in arms])
    write_json(R.p("holdout_metrics.json"), {"frozen": frozen, "changed_files": bad, "meta": R.meta, "notes": notes,
                                             "arms": [{k: v for k, v in a.items() if k != "_pred"} for a in arms]}, default=str)
    print(f"[analyze] hold-out {'FROZEN ✓' if frozen else 'NOT FROZEN ✗ ' + str(bad)} · {len(arms)} arms → {R.p('HOLDOUT_SUMMARY.md')}")


def f3(x, d=3): return "–" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))
def f0(x): return "–" if x is None else f"{x:.0f}"
def ci(t): return "" if not t or t[0] is None else f" [{t[0]:.2f}–{t[1]:.2f}]"


def render(R, arms, thr, probe, lad, test, thr2=None, xs=None):
    env = R.meta.get("env", {}); lm = R.meta.get("llama_models", {})
    expected = len(test); complete = all(a["n"] == expected for a in arms) and any(a.get("kind") == "pk" for a in arms) and any(a.get("kind") == "gemma" for a in arms)
    L = [f"# Gate B（Colab）— P+K vs Gemma 4 E2B 意圖解析模組比較（{'COMPLETE' if complete else 'PARTIAL'}）\n",
         f"產生時間 {time.strftime('%F %T')} · run `{R.path}` · seed={R.meta['seed']} n_per={R.meta['n_per']} · 案例 {len(R.cases)}（cal {sum(c['split'] == 'cal' for c in R.cases)} / test {expected} / 安全補充集 {sum(c['split'] == 'extra' for c in R.cases)}）· **所有數字只看 test**\n",
         f"GPU：**{env.get('gpu')}**（cc {env.get('compute_cap')}，{env.get('gpu_mem')}，driver {env.get('driver')}）· Kev：{env.get('kev_model', '?')}（dtype {env.get('kev_dtype', '?')}，kev commit {env.get('kev_commit', '?')}）· llama.cpp {env.get('llama_commit', '?')}\n",
         "> 金標為合成資料（gate_data.py），驗證的是接線與穩健性，不代表真實使用者準確率。test 只有約 100 句，安全正例很少：差距小於信賴區間時不要當成差異。\n",
         "> 延遲只在這張 Colab GPU 上、只看**相對倍率**與**輸出 token 數**；絕對值、併發、排隊、瓶頸都**不可**移植到 MI300X 或正式環境。\n",
         "> Gemma 臂皆為量化模型：準確率屬「非原始精度」。" + "；".join(f"{q}＝{label_note(q)}（`{os.path.basename((lm.get(q) or {}).get('model_path') or '?')}`）" for q in sorted(lm)) + "\n"]
    if probe:
        L.append(f"Kev noul 方向探測：安全句平均 p={f3(probe.get('true_mean'))}、非安全句平均 p={f3(probe.get('false_mean'))} → noul_true_high={probe.get('noul_true_high')}（Kev 文件：noul = 「是」的機率）\n")
    if thr:
        L.append(f"P+K v1 門檻（只用 cal 決定，cal 有效 {thr['cal_n']} 句、安全正例 {thr['cal_safety_pos']}）：safety={thr['safety']}，intent={thr['intent']}，avoid={thr['avoid']}\n")
    if thr2:
        L.append(f"P+K v2 門檻（cal）：safety={thr2['safety']}（不含急症句調出）、emergency={thr2['emergency']}（cal 急症正例只有 {thr2['cal_emergency_pos']} 句，門檻很不穩；此門檻在 cal 非安全句的誤報率 {thr2.get('emergency_cal_fpr')}{' ⚠ 急症題分不開，v2 的安全誤報會很高' if (thr2.get('emergency_cal_fpr') or 0) > 0.2 else ''}）、intent={thr2['intent']}；unclear 改為「沒有其他意圖時才是 unclear」\n")
    L.append("## 1. 主表（test）\n")
    L.append("|臂|n|安全 recall（FN）|急症漏判|意圖 micro-F1|exact-set|預算 值+角色|避開成分找到|嚴重錯誤（句數）|全對 [95% CI]|JSON 解析失敗|輸出 tok p50|延遲 p50 ms|×P+K|")
    L.append("|--|--|--|--|--|--|--|--|--|--|--|--|--|--|")
    for a in arms:
        s, it, b, av = a["safety"], a["intent"], a["budget"], a["avoid"]
        jf = "–" if a.get("kind") != "gemma" else f"{a['parse_fail']}/{a['n']}"
        if a.get("reasoning_seen"): jf += f" †{a['reasoning_seen']}"
        ot = (a.get("out_tok") or {}).get("p50")
        L.append(f"|{a['name']}|{a['n']}{'' if a['n'] == a['n_test'] else '⚠'}|{f3(s['recall'], 2)} ({s['fn']}/{s['pos']})|{s['emergency_missed']}/{s['emergency_n']}|{f3(it['f1'])}|{f3(it['exact'], 2)}|"
                 f"{b['value_role_ok']}/{b['n']}|{av['found']}/{av['n']}|{a['severe']} ({a['severe_cases']})|{a['full_ok']}/{a['n']}{ci(a['full_ok_ci'])}|{jf}|{f0(ot)}|{f0(a['lat']['total_ms']['p50'])}|"
                 f"{'–' if a.get('lat_ratio_vs_pk') is None else format(a['lat_ratio_vs_pk'], '.1f') + '×'}|")
    L.append("\n† = 有幾句輸出含 thinking（`--reasoning off` 沒擋住）：這些句的 token 數與延遲不代表正常解碼，用 `gemma-collect --redo-thinking` 重收。")
    L.append("\n⚠ = 該臂尚未跑完全部 test 句（部分結果）。P+K 延遲＝Parser＋一次 Kev 請求（client 端 wall）；Gemma 延遲＝一次 llama-server 請求（VU=1、循序）。\n")
    L.append("## 2. 錯誤類型計數（test；severe＝會改資料或安全行為）\n")
    codes = []
    for a in arms:
        for k, v in a["errors"].items():
            if (k, v["sev"]) not in codes: codes.append((k, v["sev"]))
    codes.sort(key=lambda x: ({"severe": 0, "moderate": 1, "minor": 2}[x[1]], x[0]))
    L.append("|錯誤|嚴重度|" + "|".join(a["name"] for a in arms) + "|"); L.append("|--|--|" + "--|" * len(arms))
    for k, sv in codes: L.append(f"|{k}|{sv}|" + "|".join(str(a["errors"].get(k, {}).get("n", 0)) for a in arms) + "|")
    L.append("\n## 3. 細項（test）\n")
    L.append("|臂|安全 FPR（FP）|意圖 P|意圖 R|預算值對|假預算|避開成分誤報|訴求對（find_product）|")
    L.append("|--|--|--|--|--|--|--|--|")
    for a in arms:
        s, it, b, av, co = a["safety"], a["intent"], a["budget"], a["avoid"], a["concern"]
        L.append(f"|{a['name']}|{f3(s['fpr'], 2)} ({s['fp']}/{s['neg']})|{f3(it['p'])}|{f3(it['r'])}|{b['value_ok']}/{b['n']}|{b['false']}/{b['n_without']}|{av['fp']}/{av['fp_n']}|{co['ok']}/{co['n']}|")
    L.append("\n各意圖 F1：\n\n|臂|" + "|".join(LABELS) + "|"); L.append("|--|" + "--|" * len(LABELS))
    for a in arms: L.append(f"|{a['name']}|" + "|".join(f3(a["intent"]["per_label_f1"].get(l), 2) for l in LABELS) + "|")
    gm = [a for a in arms if a.get("kind") == "gemma"]
    if gm:
        L.append("\n## 4. Gemma 輸出品質與 token\n")
        L.append("|臂|解析失敗（種類）|schema 違規|被截斷（finish=length）|出現 thinking|HTTP 錯誤|值正規化次數|丟棄的值|prompt tok p50|cache 命中 tok p50|輸出 tok p50 / mean / p95|")
        L.append("|--|--|--|--|--|--|--|--|--|--|--|")
        for a in gm:
            ot = a["out_tok"]
            L.append(f"|{a['name']}|{a['parse_fail']} {a['parse_fail_kinds'] or ''}|{a['schema_violations']}|{a['truncated']}|{a['reasoning_seen']}|{a['http_errors']}|{a['normalised']}|{a['dropped']}|"
                     f"{f0(a['lat']['prompt_n']['p50'])}|{f0(a['lat']['cache_n']['p50'])}|{f0(ot['p50'])} / {f0(ot['mean'])} / {f0(ot['p95'])}|")
        L.append("\nprompt_only 臂的解析失敗算作錯誤（判定全空）；json 臂由 llama.cpp 的 JSON-schema 文法受限解碼。正規化＝把同義詞或中文成分名對應到代碼（見 gemma_arms.to_decision）。\n")
    L.append("## 5. 延遲拆解（VU=1，循序；只看相對）\n")
    L.append("|臂|wall p50|wall p95|server 推論 p50|排隊估計 p50|prefill p50|decode p50|decode tok/s p50|Parser p50|Kev 題數 p50|")
    L.append("|--|--|--|--|--|--|--|--|--|--|")
    for a in arms:
        l = a["lat"]; g = lambda k, q="p50": (l.get(k) or {}).get(q)
        L.append(f"|{a['name']}|{f0(g('total_ms'))}|{f0(g('total_ms', 'p95'))}|{f0(g('server_ms'))}|{f0(g('queue_est_ms'))}|{f0(g('prompt_ms'))}|{f0(g('predicted_ms'))}|{f0(g('tok_s'))}|{f3(g('parser_ms'), 2)}|{f0(g('nq'))}|")
    L.append("\nserver 推論：Kev 自報 `latency_ms`；llama-server `timings.prompt_ms + predicted_ms`。排隊估計＝client wall − server 推論（含 HTTP、序列化）。\n")
    if lad:
        L.append("## 6. 併發階梯（Colab 上不可移植，只供形狀參考）\n")
        L.append("|目標|VU|n|錯誤|rps|wall p50|wall p95|server p50|排隊 p50|輸出 tok p50|各 VU wall p50|GPU util max|")
        L.append("|--|--|--|--|--|--|--|--|--|--|--|--|")
        for tag, r in lad.items():
            for lv, x in sorted(r["levels"].items(), key=lambda kv: int(kv[0])):
                L.append(f"|{tag}|{lv}|{x['n']}|{x['errors']}|{x['rps']}|{f0(x['wall_ms']['p50'])}|{f0(x['wall_ms']['p95'])}|{f0(x['server_ms']['p50'])}|{f0(x['queue_est_ms']['p50'])}|"
                         f"{f0(x['out_tok']['p50'])}|{', '.join(f0(v) for v in x['per_vu_wall_p50'].values())}|{f0((x.get('gpu') or {}).get('max_util_pct'))}|")
        L.append("")
    pv = [a for a in arms if a.get("paired_vs_pk")]
    if pv:
        L.append("## 7. 與 P+K 成對比較（同一批 test 句；McNemar 精確檢定）\n")
        L.append("|臂|共同句數|P+K 全對、此臂錯|此臂全對、P+K 錯|p|P+K 無嚴重、此臂有|此臂無嚴重、P+K 有|p（嚴重）|")
        L.append("|--|--|--|--|--|--|--|--|")
        for a in pv:
            x = a["paired_vs_pk"]
            L.append(f"|{a['name']}|{x['n']}|{x['pk_ok_arm_wrong']}|{x['arm_ok_pk_wrong']}|{f3(x['p'])}|{x['pk_clean_arm_severe']}|{x['arm_clean_pk_severe']}|{f3(x['p_severe'])}|")
        L.append("")
    L.append("## 8. 各組全對句數（test）\n")
    groups = sorted({g for a in arms for g in a["by_group"]})
    L.append("|臂|" + "|".join(groups) + "|"); L.append("|--|" + "--|" * len(groups))
    for a in arms: L.append(f"|{a['name']}|" + "|".join(a["by_group"].get(g, "–") for g in groups) + "|")
    L.append("\n## 9. 漏判的安全句（test）\n")
    for a in arms:
        if a["safety"]["fn_texts"]: L.append(f"- **{a['name']}**：" + "；".join(a["safety"]["fn_texts"]))
    pp = [a for a in arms if a.get("kind") == "prod_path"]
    if pp:
        L.append("\n## 9a. Production 現行路徑（決定性層在前，只有剩下的句子交給 LLM；prod_replay.py）\n")
        L.append("|臂|交給 LLM 的比例|路徑分布（test）|平均延遲 ms|p50|p95|")
        L.append("|--|--|--|--|--|--|")
        for a in pp:
            L.append(f"|{a['name']}|{a['llm_share']:.0%}|{'、'.join(f'{k} {v}' for k, v in sorted(a['paths'].items(), key=lambda kv: -kv[1]))}|{f0(a['lat']['mean_ms'])}|{f0(a['lat']['total_ms']['p50'])}|{f0(a['lat']['total_ms']['p95'])}|")
        L.append("\n說明：決定性層（第一層路由、resolve_product_turn、fast path）不呼叫模型，延遲約 0 ms；只有路徑為 LLM 的句子用該臂的實際 wall time。重播條件＝新對話、無歷史；未重播 input guard、低信心重試、記憶補全。安全＝production 的判定方式（第一層急症，或 FIND_PRODUCT 且原句含 SAFETY_KEYWORDS）。\n")
    if xs:
        L.append("\n## 9b. 安全補充集（extra_safety.py；只評安全，不調門檻）\n")
        L.append("|臂|急症→safety|急症 emergency 旗標|詢問型安全→safety|非安全句誤報|漏判|")
        L.append("|--|--|--|--|--|--|")
        for name, x in xs.items():
            ef = "–" if not x["emergency_flag"] else f"{x['emergency_flag'][0]}/{x['emergency_flag'][1]}"
            L.append(f"|{name}|{x['emergency_safety'][0]}/{x['emergency_safety'][1]}|{ef}|{x['ask_safety'][0]}/{x['ask_safety'][1]}|{x['neg_false'][0]}/{x['neg_false'][1]}|{'；'.join(x['missed'][:5])}{' …' if len(x['missed']) > 5 else ''}|")
        L.append("")
    L.append("\n## 10. 結構限制提醒\n\n- `legacy` 臂用 tests/fixture 的 IntentParseResult：沒有安全、記錄、閒聊、避開成分、多意圖欄位，相關項目必然失分（這正是 Legacy JSON 路徑的結構問題）。\n"
             "- `legacy_system.txt` 是依欄位寫的替身 prompt，不是 production 原文；換成原文後重跑 legacy 臂即可（prompt 簽章會變，舊列不要混用）。\n"
             "- P+K 的預算數值只來自 Parser 候選；Kev 只判斷角色。Gemma 直接輸出數值。\n"
             "- 決定性第一層（prod_dialogue_router）不在這個比較內：兩邊都是從意圖解析階段開始。\n")
    L.append(f"\n檔案：`metrics.json`（全部數字）、`per_case.csv`（每句每臂）、`errors/*.tsv`（每臂錯誤明細）、`kev/raw.jsonl`、`gemma/<quant>/<arm>.jsonl`、`latency/*.json`。\n")
    return "\n".join(L) + "\n"


def cmd_env(A, R):
    R.meta["env"].update(gpu_facts())
    for kv in A.set or []:
        k, _, v = kv.partition("="); R.meta["env"][k] = v
    R.save(); print(json.dumps(R.meta["env"], ensure_ascii=False, indent=1))


def cmd_status(A, R):
    test = [c for c in R.cases if c["split"] in ("test", "holdout")]; cal = [c for c in R.cases if c["split"] == "cal"]
    for lab in kev_labels(R):
        k = read_jsonl(R.p(*kev_dir(lab), "raw.jsonl"))
        print(f"kev {lab}: cal {sum(c['id'] in k for c in cal)}/{len(cal)} · test {sum(c['id'] in k for c in test)}/{len(test)}")
    gd = os.path.dirname(R.p("gemma", "x"))
    for q in sorted(os.listdir(gd)) if os.path.isdir(gd) else []:
        for f in sorted(x for x in os.listdir(os.path.join(gd, q)) if x.endswith(".jsonl")):
            g = read_jsonl(os.path.join(gd, q, f)); print(f"gemma {q} {f[:-6]}: test {sum(c['id'] in g for c in test)}/{len(test)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["env", "kev-collect", "gemma-collect", "latency", "analyze", "status"])
    ap.add_argument("--run", required=True); ap.add_argument("--dataset", default="gate_data", choices=["gate_data", "router_gold"],
                    help="router_gold = frozen hold-out (holdout/SCORING.md): no tuning, analysis uses holdout/frozen_thresholds.json")
    ap.add_argument("--seed", type=int, default=7); ap.add_argument("--n-per", type=int, default=24)
    ap.add_argument("--kev-url", default="http://127.0.0.1:8230"); ap.add_argument("--llm-url", default="http://127.0.0.1:8350")
    ap.add_argument("--quant", default="q8_0"); ap.add_argument("--force-quant-label", action="store_true")
    ap.add_argument("--arms", default=",".join(GM.DEFAULT_ARMS)); ap.add_argument("--split", default="test", choices=["test", "all"])
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--max-tokens", type=int, default=512); ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--vus", type=int, default=1); ap.add_argument("--target", default="kev", choices=["kev", "gemma"]); ap.add_argument("--levels", default="1,2,4")
    ap.add_argument("--n", type=int, default=48); ap.add_argument("--redo", action="store_true"); ap.add_argument("--min-recall-safety", type=float, default=0.99)
    ap.add_argument("--set", action="append", help="env: key=value facts to record"); ap.add_argument("--no-analyze", action="store_true")
    ap.add_argument("--kev-label", default="0.8b", help="which Kev model the Kev server runs: 0.8b (production candidate, results in kev/) or e.g. 4b (results in kev/4b/)")
    ap.add_argument("--no-extra", action="store_true", help="gemma-collect: skip the safety supplement (extra_safety.py)")
    ap.add_argument("--redo-thinking", action="store_true", help="gemma-collect: re-collect rows whose output contains leaked thinking")
    A = ap.parse_args()
    signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig)
    # target guards BEFORE any file is created
    K.check_base(A.kev_url); llm_check(A.llm_url)
    if A.dataset == "router_gold" and A.cmd == "latency": sys.exit("[gateb] the hold-out run has no latency ladder (SCORING.md §5 M7 uses the collection timings)")
    R = RunDir(A.run, A.seed, A.n_per, dataset=A.dataset)
    fn = {"env": cmd_env, "kev-collect": cmd_kev_collect, "gemma-collect": cmd_gemma_collect, "latency": cmd_latency, "analyze": cmd_analyze, "status": cmd_status}[A.cmd]
    code = 0
    try:
        fn(A, R)
        if A.cmd in ("kev-collect", "gemma-collect", "latency"): R.status(f"DONE {A.cmd}" + (f" {A.quant}" if A.cmd != "kev-collect" else ""))
    except KeyboardInterrupt:
        R.status(f"INTERRUPTED {A.cmd} (partial results valid; rerun the same command to resume)"); print(f"\n[gateb] interrupted — rerun the same command to resume", flush=True); code = 130
    if A.cmd in ("kev-collect", "gemma-collect", "latency") and not A.no_analyze:
        STOP[0] = False
        try: cmd_analyze(A, R)
        except Exception as e: print(f"[analyze] failed: {type(e).__name__}: {e}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
