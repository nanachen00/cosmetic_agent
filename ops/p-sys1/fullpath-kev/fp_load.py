#!/usr/bin/env python3
"""fp_load — closed-loop VU load + per-VU timing against a running SHADOW backend (:8210..8219), comparing dispatch arms.

For every arm in --arms (off = original LLM intent, kev_route, kev_slots) and every VU level in --levels:
  * flips the overlay control file (control/kev_dispatch.json) to the arm (takes effect on the next request, no restart),
  * runs N virtual users, each a conversation of --turns gold queries (closed loop, think-time optional),
  * records EVERY turn (turns.jsonl: vu, turn, t_start, ttfb, total, status) -> per-VU rows (per_vu.csv),
  * joins the overlay trace (kev_ms, kev_server_ms = Kev-side inference, kev_ms-kev_server_ms = queue+transport,
    route decision, parser_ms) by q_sha -> dispatch accuracy vs gold + Kev latency split,
  * optionally scrapes a vLLM /metrics (8200..8299 only) before/after the level: queue vs prefill vs decode deltas.
Checkpoint: done.json holds finished (arm,vu) units; --resume skips them. Partial results are always summarised (SUMMARY.md) — also on SIGTERM.
Safety: base-url port must be 8210-8219; ports 8000/8001/8002/8003/8010 are refused outright.
"""
import argparse, csv, hashlib, json, os, random, re, signal, statistics, sys, threading, time, urllib.request, http.client
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
FORBIDDEN = {8000, 8001, 8002, 8003, 8010}


def die(m): sys.exit(f"[REFUSED] {m}")
def pct(xs, q):
    xs = sorted(xs)
    if not xs: return None
    if len(xs) == 1: return xs[0]
    h = (len(xs) - 1) * q; lo = int(h); hi = min(lo + 1, len(xs) - 1); return xs[lo] + (h - lo) * (xs[hi] - xs[lo])
def atomic(p: Path, text: str):
    t = p.with_name(p.name + ".tmp"); t.write_text(text, encoding="utf-8")
    with open(t, "rb") as f: os.fsync(f.fileno())
    os.replace(t, p)


def check_urls(a):
    u = urlparse(a.base_url)
    if u.hostname not in ("127.0.0.1", "localhost") or not u.port: die("base-url must be http://127.0.0.1:<port>")
    if u.port in FORBIDDEN or not 8210 <= u.port <= 8219: die(f"port {u.port}: only the Shadow backend range 8210-8219 is allowed")
    if a.vllm_metrics:
        m = urlparse(a.vllm_metrics)
        if m.hostname not in ("127.0.0.1", "localhost") or not m.port or not 8200 <= m.port <= 8299: die("vllm-metrics must be 127.0.0.1:8200-8299/metrics")


def load_cases(a):
    g = json.loads(Path(a.gold).read_text(encoding="utf-8"))["cases"]
    rng = random.Random(a.seed); by = {}
    for c in g: by.setdefault(c["route"], []).append(c)
    pool = []
    for r, cs in by.items(): pool += rng.sample(cs, min(len(cs), a.per_route))
    rng.shuffle(pool); return pool


def set_arm(ctl: Path, arm: str, extra: dict):
    d = json.loads(ctl.read_text()) if ctl.exists() else {}
    d.update(extra); d["arm"] = arm; atomic(ctl, json.dumps(d, indent=1))
    time.sleep(0.05)


def scrape(url):
    if not url: return {}
    try:
        txt = urllib.request.urlopen(url, timeout=5).read().decode()
    except Exception: return {}
    out = {}
    for fam in ("request_queue_time_seconds", "request_prefill_time_seconds", "request_decode_time_seconds", "request_inference_time_seconds", "e2e_request_latency_seconds"):
        s = c = 0.0
        for ln in txt.splitlines():
            if ln.startswith("vllm:" + fam + "_sum"): s += float(ln.rsplit(" ", 1)[1])
            elif ln.startswith("vllm:" + fam + "_count"): c += float(ln.rsplit(" ", 1)[1])
        out[fam] = (s, c)
    return out


class Turn:
    def __init__(self, a, acct):
        u = urlparse(a.base_url); self.host, self.port = u.hostname, u.port; self.a = a
        self.headers = {"Content-Type": "application/json", **acct.get("headers", {})}
        if acct.get("cookie"): self.headers["Cookie"] = acct["cookie"]
    def send(self, text, conv):
        body = self.a.body_template.replace("{text}", json.dumps(text, ensure_ascii=False)[1:-1]).replace("{conv}", conv)
        c = http.client.HTTPConnection(self.host, self.port, timeout=self.a.timeout)
        t0 = time.perf_counter(); wall0 = time.time()
        try:
            c.request("POST", self.a.endpoint, body=body.encode("utf-8"), headers=self.headers)
            r = c.getresponse(); ttfb = time.perf_counter() - t0; data = r.read(); tot = time.perf_counter() - t0
            return {"status": r.status, "ttfb_s": ttfb, "total_s": tot, "bytes": len(data), "t_wall": wall0, "err": ""}
        except Exception as e:
            return {"status": 0, "ttfb_s": None, "total_s": time.perf_counter() - t0, "bytes": 0, "t_wall": wall0, "err": type(e).__name__}
        finally: c.close()


def run_level(a, arm, vu, cases, accts, rows_f):
    res = []; lock = threading.Lock(); stop_at = time.time() + a.max_level_seconds
    def worker(i):
        cl = Turn(a, accts[i % len(accts)]); rng = random.Random(a.seed * 1000 + i); conv = f"fp-{arm}-{vu}-{i}-{int(time.time())}"
        for t in range(a.turns):
            if time.time() > stop_at: break
            c = cases[(i * a.turns + t) % len(cases)]
            r = cl.send(c["query"], conv); r.update(arm=arm, level_vu=vu, vu=i, turn=t, q_sha=hashlib.sha256(c["query"].encode()).hexdigest(), gold=c["route"])
            with lock: res.append(r); rows_f.write(json.dumps(r, ensure_ascii=False) + "\n"); rows_f.flush()
            if a.think_s: time.sleep(rng.uniform(0.5, 1.5) * a.think_s)
    th = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(vu)]
    t0 = time.time(); [x.start() for x in th]; [x.join() for x in th]
    return res, t0, time.time()


def kev_rows(trace: Path, t0, t1):
    out = []
    try:
        for ln in trace.read_text(encoding="utf-8").splitlines():
            r = json.loads(ln)
            if t0 - 1 <= r["ts"] <= t1 + 1: out.append(r)
    except Exception: pass
    return out


def summarize_level(a, arm, vu, res, t0, t1, kev, vb, va):
    ok = [r for r in res if r["status"] == 200]; tot = [r["total_s"] for r in ok]
    row = {"arm": arm, "vu": vu, "turns": len(res), "ok": len(ok), "err": len(res) - len(ok), "success": round(len(ok) / len(res), 4) if res else None,
           "wall_s": round(t1 - t0, 2), "turns_per_s": round(len(res) / max(t1 - t0, 1e-9), 3),
           "p50_s": pct(tot, .5), "p95_s": pct(tot, .95), "p99_s": pct(tot, .99), "ttfb_p50_s": pct([r["ttfb_s"] for r in ok if r["ttfb_s"] is not None], .5)}
    if kev:
        km = [r["kev_ms"] for r in kev if "kev_ms" in r]; ks = [r["kev_server_ms"] for r in kev if r.get("kev_server_ms") is not None]
        q = [r["kev_ms"] - r["kev_server_ms"] for r in kev if "kev_ms" in r and r.get("kev_server_ms") is not None]
        gold = {r["q_sha"]: r["gold"] for r in res}; hit = tot_ = 0
        for r in kev:
            g = gold.get(r["q_sha"])
            if g: tot_ += 1; hit += int(r.get("route") == g)
        row.update(kev_n=len(kev), kev_err=sum(1 for r in kev if str(r.get("why", "")).startswith("kev_error")), kev_ms_p50=pct(km, .5), kev_ms_p95=pct(km, .95),
                   kev_infer_ms_p50=pct(ks, .5), kev_queue_transport_ms_p50=pct(q, .5), kev_queue_transport_ms_p95=pct(q, .95),
                   parser_ms_p50=pct([r["parser_ms"] for r in kev if "parser_ms" in r], .5), route_acc=round(hit / tot_, 4) if tot_ else None,
                   to_clarify=sum(1 for r in kev if r.get("route") == "clarify"))
    for fam in ("request_queue_time_seconds", "request_prefill_time_seconds", "request_decode_time_seconds", "request_inference_time_seconds", "e2e_request_latency_seconds"):
        if fam in vb and fam in va:
            ds, dc = va[fam][0] - vb[fam][0], va[fam][1] - vb[fam][1]; row["vllm_" + fam.replace("request_", "").replace("_seconds", "") + "_mean_s"] = round(ds / dc, 4) if dc > 0 else None
    return row


def write_reports(out: Path, rows, per_vu):
    if rows:
        keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("arm", "vu"), k))
        with open(out / "levels.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, keys); w.writeheader(); w.writerows(rows)
    if per_vu:
        keys = list(per_vu[0])
        with open(out / "per_vu.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, keys); w.writeheader(); w.writerows(per_vu)
    md = ["# Full-path Kev-dispatch load result (PARTIAL results are valid; see status line)", "", "| arm | VU | turns | success | p50 s | p95 s | p99 s | turns/s | Kev p50 ms | Kev infer p50 ms | Kev queue+transport p50 ms | route acc | →clarify |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f2 = lambda v, d=2: "-" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v))
    for r in sorted(rows, key=lambda r: (r["arm"], r["vu"])):
        md.append(f"| {r['arm']} | {r['vu']} | {r['turns']} | {f2(r['success'], 3)} | {f2(r['p50_s'])} | {f2(r['p95_s'])} | {f2(r['p99_s'])} | {f2(r['turns_per_s'])} | {f2(r.get('kev_ms_p50'), 1)} | {f2(r.get('kev_infer_ms_p50'), 1)} | {f2(r.get('kev_queue_transport_ms_p50'), 1)} | {f2(r.get('route_acc'), 3)} | {r.get('to_clarify', '-')} |")
    md += ["", "Notes: p-values are over HTTP-200 turns; `Kev queue+transport` = client-seen Kev time minus Kev-reported inference time; `route acc` joins overlay trace to gold by query hash.",
           "Rows with `off` are the original LLM-intent path measured through the same backend (same hook, passthrough)."]
    atomic(out / "SUMMARY.md", "\n".join(md) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8210"); ap.add_argument("--endpoint", default="/api/chat/message")
    ap.add_argument("--body-template", default='{"message": "{text}", "conversation_id": "{conv}"}', help="UNVERIFIED default — copy the real body from a harness smoke request audit")
    ap.add_argument("--accounts-file", required=True, help='json: [{"name":"u1","headers":{"Authorization":"Bearer ..."}, "cookie":"..."}]  (test accounts only)')
    ap.add_argument("--gold", default=str(HERE.parent / "datasets/router_gold.json")); ap.add_argument("--per-route", type=int, default=20); ap.add_argument("--seed", type=int, default=20261018)
    ap.add_argument("--arms", default="off,kev_route,kev_slots"); ap.add_argument("--levels", default="1,2,4,8,16"); ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--think-s", type=float, default=0.0); ap.add_argument("--timeout", type=float, default=120); ap.add_argument("--max-level-seconds", type=float, default=900)
    ap.add_argument("--control", default=str(HERE / "control/kev_dispatch.json")); ap.add_argument("--trace", default=None)
    ap.add_argument("--vllm-metrics", default=None, help="e.g. http://127.0.0.1:8200/metrics (read-only)")
    ap.add_argument("--out", default=None); ap.add_argument("--resume", action="store_true")
    a = ap.parse_args(); check_urls(a)
    ctl = Path(a.control); trace = Path(a.trace) if a.trace else Path(json.loads(ctl.read_text()).get("trace", HERE / "logs/kev_dispatch_trace.jsonl"))
    base = HERE / "runs"; out = Path(a.out) if a.out else (sorted(base.glob("fp-*"))[-1] if a.resume and base.exists() and list(base.glob("fp-*")) else base / time.strftime("fp-%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    accts = json.loads(Path(a.accounts_file).read_text()); accts = accts["accounts"] if isinstance(accts, dict) else accts
    cases = load_cases(a); done_p = out / "done.json"; done = set(json.loads(done_p.read_text())) if done_p.exists() else set()
    rows = json.loads((out / "levels.json").read_text()) if (out / "levels.json").exists() else []; per_vu = json.loads((out / "per_vu.json").read_text()) if (out / "per_vu.json").exists() else []
    atomic(out / "config.json", json.dumps({k: v for k, v in vars(a).items() if k != "accounts_file"}, indent=1, default=str))
    interrupted = []
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    rows_f = open(out / "turns.jsonl", "a", encoding="utf-8")
    status = "COMPLETE"
    try:
        for arm in a.arms.split(","):
            for vu in [int(x) for x in a.levels.split(",")]:
                key = f"{arm}|{vu}"
                if key in done: print(f"[checkpoint] reuse {key}"); continue
                set_arm(ctl, arm, {"trace": str(trace), "kev_url": json.loads(ctl.read_text()).get("kev_url", "http://127.0.0.1:8230")})
                vb = scrape(a.vllm_metrics); res, t0, t1 = run_level(a, arm, vu, cases, accts, rows_f); va = scrape(a.vllm_metrics)
                kev = kev_rows(trace, t0, t1) if arm != "off" else []
                rows.append(summarize_level(a, arm, vu, res, t0, t1, kev, vb, va))
                for i in range(vu):
                    mine = [r for r in res if r["vu"] == i and r["status"] == 200]; allm = [r for r in res if r["vu"] == i]
                    per_vu.append({"arm": arm, "level_vu": vu, "vu": i, "turns": len(allm), "ok": len(mine), "p50_s": pct([r["total_s"] for r in mine], .5), "p95_s": pct([r["total_s"] for r in mine], .95),
                                   "max_s": max([r["total_s"] for r in mine], default=None), "first_turn_s": mine[0]["total_s"] if mine else None})
                done.add(key); atomic(done_p, json.dumps(sorted(done))); atomic(out / "levels.json", json.dumps(rows)); atomic(out / "per_vu.json", json.dumps(per_vu))
                write_reports(out, rows, per_vu); print(f"[{key}] turns={rows[-1]['turns']} ok={rows[-1]['ok']} p95={rows[-1]['p95_s']}")
    except KeyboardInterrupt:
        status = "INTERRUPTED"
    finally:
        try: set_arm(ctl, "off", {})                       # always leave the overlay passthrough
        except Exception: pass
        rows_f.close(); write_reports(out, rows, per_vu)
        atomic(out / "STATUS.txt", f"{status}\nfinished units: {sorted(done)}\n"); print(f"{status}: {out}")
    return 130 if status == "INTERRUPTED" else 0


if __name__ == "__main__":
    sys.exit(main())
