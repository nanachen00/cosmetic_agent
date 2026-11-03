#!/usr/bin/env python3
"""S3 影子比對（T7）：同一批請求同時算舊 LLM router 與 P+Sys1 決策，只記錄、不影響回應。

做法：overlay 設 arm=shadow → 回應仍由舊 LLM parse_intent 決定；P+Sys1 決策在背景執行緒算完寫進 trace（llm_route 與 sys1_route 同一列）。
  drive    對「已經在跑」的 Shadow backend（127.0.0.1:8210-8219）送情境句；暫時把控制檔設成 shadow，結束（含 Ctrl-C/SIGTERM）一律還原成原本的 arm。
           送出前後都讀 /_shadow/provenance（shadow_entry.py）：orchestrator 不是 shadow 複本、overlay 沒生效、或有模組／.env 來自正式環境就拒絕（前）或判無效（後，rc=4）。
  analyze  讀 trace，輸出 SUMMARY.md / s3_metrics.json / disagreements.csv / safety_llm_only.csv。

請求來源只能是 ops/agent-profile/scenarios.json 的合成情境（--source synthetic）或負責人提供、有同意紀錄的流量（--source consented --consent-note "…"）。

  python3 s3_shadow.py drive   --base-url http://127.0.0.1:8210 --scenarios <情境檔> --control control/sys1_dispatch.json --trace-text
  python3 s3_shadow.py analyze --trace logs/sys1_dispatch_trace.jsonl --source synthetic --out runs/s3-YYYYMMDD [--since TS]
請求格式取自 production 原始碼（analysis-source-20261013T163259Z：app/schemas/chat.py ChatRequest、tests/load/chat_load.py 訪客流程）：
每個情境一個訪客 session（POST /api/auth/guest-session），再 POST /api/chat/message {"user_id","conversation_id","message","flow":"auto"}。不需要帳號檔。
"""
from __future__ import annotations
import argparse, csv, hashlib, http.cookiejar, json, math, os, signal, sys, time, urllib.request, uuid
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROUTES = ["safety", "skin_tracking", "product", "knowledge", "conversation", "clarify"]
FORBIDDEN = {8000, 8001, 8002, 8003, 8010, 8200}


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs: return None
    h = (len(xs) - 1) * q; lo = int(h); hi = min(lo + 1, len(xs) - 1); return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def wilson(k, n, z=1.96):
    if not n: return (None, None)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)); return ((c - h) / d, (c + h) / d)


def atomic_json(p: Path, obj):
    t = p.with_name(p.name + ".tmp"); t.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8"); os.replace(t, p)


# ----------------------------------------------------------------------------- scenarios
def load_scenarios(path):
    """接受：["句子", …] | [{"text"}] | [{"turns":[…]}] | {"scenarios":[…]} | [{"messages":[{"role":"user","content"}]}]。其他格式拒絕（不猜）。"""
    j = json.loads(Path(path).read_text(encoding="utf-8")); items = j.get("scenarios", j.get("cases")) if isinstance(j, dict) else j
    if not isinstance(items, list): sys.exit(f"[s3] {path}: 不認得的格式（需要 list 或 {{'scenarios': [...]}}）")
    convs = []
    for i, s in enumerate(items):
        if isinstance(s, str): turns = [s]
        elif isinstance(s, dict) and isinstance(s.get("text") or s.get("query"), str): turns = [s.get("text") or s.get("query")]
        elif isinstance(s, dict) and isinstance(s.get("turns"), list): turns = [t if isinstance(t, str) else (t.get("text") or t.get("content")) for t in s["turns"]]
        elif isinstance(s, dict) and isinstance(s.get("messages"), list): turns = [m.get("content") for m in s["messages"] if m.get("role") == "user"]
        else: sys.exit(f"[s3] {path}[{i}]: 不認得的情境格式 {str(s)[:120]}")
        turns = [t for t in turns if isinstance(t, str) and t.strip()]
        if turns: convs.append({"id": str(s.get("id", i)) if isinstance(s, dict) else str(i), "turns": turns})
    return convs


def guest_session(base, timeout):
    """cosmetic-agent 的訪客流程（tests/load/chat_load.py）：POST /api/auth/guest-session → {"user_id"} ＋ session cookie。"""
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    req = urllib.request.Request(base.rstrip("/") + "/api/auth/guest-session", data=b"{}", headers={"Content-Type": "application/json"})
    with op.open(req, timeout=timeout) as r: uid = json.loads(r.read())["user_id"]
    if not isinstance(uid, str) or not uid: raise ValueError("guest-session returned no user_id")
    return op, uid


def provenance(base, timeout=30):
    """shadow_entry.py 的來源報告（orchestrator 是否來自 shadow 複本且 overlay 生效、有沒有模組或 .env 來自正式環境）。"""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/_shadow/provenance", timeout=timeout) as r: rep = json.loads(r.read())
    except Exception as e: return False, f"讀不到 /_shadow/provenance（{type(e).__name__}: {str(e)[:120]}）；backend 要用 shadow_stack.py up 啟動"
    o = rep.get("orchestrator") or {}
    return rep.get("verdict") == "ok", f"verdict={rep.get('verdict')} orchestrator={o.get('file')} problems={rep.get('problems') or []}"


def cmd_drive(a):
    u = urlparse(a.base_url)
    if u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port or u.port in FORBIDDEN or not 8210 <= u.port <= 8219:
        sys.exit("[REFUSED] base-url 必須是 http://127.0.0.1:8210-8219（Shadow backend）")
    ok, msg = provenance(a.base_url)
    if not ok: sys.exit(f"[REFUSED] shadow backend 來源檢查未通過，S3 不執行：{msg}")
    print(f"[s3] provenance ok: {msg}", flush=True)
    ctl = Path(a.control); orig = json.loads(ctl.read_text(encoding="utf-8"))
    convs = load_scenarios(a.scenarios)
    shadow = dict(orig, arm="shadow")
    if a.trace: shadow["trace"] = a.trace
    if not shadow.get("trace_text") and a.trace_text: shadow["trace_text"] = True
    restored = [False]
    def restore(*_):
        if not restored[0]: atomic_json(ctl, orig); restored[0] = True; print(f"[s3] control restored to arm={orig.get('arm')}", flush=True)
    signal.signal(signal.SIGTERM, lambda *x: (restore(), sys.exit(130))); atomic_json(ctl, shadow); t0 = time.time(); n = bad = 0
    try:
        for ci, c in enumerate(convs):
            conv = f"s3-{hashlib.sha256((c['id'] + str(t0)).encode()).hexdigest()[:10]}"
            try: op, uid = guest_session(a.base_url, a.timeout)          # one isolated guest per scenario (no accounts file needed)
            except Exception as e:
                bad += len(c["turns"]); n += len(c["turns"]); print(f"[s3] guest-session failed: {type(e).__name__}: {str(e)[:160]}", flush=True); continue
            for t in c["turns"]:
                body = json.dumps({"user_id": uid, "conversation_id": conv, "message": t, "flow": "auto"}, ensure_ascii=False).encode()
                req = urllib.request.Request(a.base_url.rstrip("/") + "/api/chat/message", data=body,
                                             headers={"Content-Type": "application/json", "X-Request-ID": "s3-" + uuid.uuid4().hex[:24], "Idempotency-Key": str(uuid.uuid4())})
                try:
                    with op.open(req, timeout=a.timeout) as r: r.read()
                except Exception as e: bad += 1; print(f"[s3] request failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
                n += 1
                if a.sleep: time.sleep(a.sleep)
    finally:
        time.sleep(a.settle); restore()
    print(f"[s3] sent {n} turns ({bad} failed) from {len(convs)} scenarios since ts={t0:.0f}; analyze with --since {t0:.0f}")
    ok, msg = provenance(a.base_url)                          # requests may import more modules: check again
    if not ok: print(f"[s3] 驅動後來源檢查未通過，這次 S3 結果無效：{msg}", flush=True); return 4
    return 0 if not bad else 3


# ----------------------------------------------------------------------------- analysis
def cmd_analyze(a):
    if a.source == "consented" and not a.consent_note: sys.exit("[s3] --source consented 需要 --consent-note（同意與去識別化紀錄）")
    rows = []
    for l in open(a.trace, encoding="utf-8"):
        try: r = json.loads(l)
        except Exception: continue
        if r.get("arm") == "shadow" and "llm_route" in r and r.get("ts", 0) >= a.since: rows.append(r)
    if not rows: sys.exit("[s3] trace 裡沒有 arm=shadow 且有 llm_route 的列")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    ok = [r for r in rows if not r.get("sys1_error")]; err = [r for r in rows if r.get("sys1_error")]
    agree = [r for r in ok if r["llm_route"] == r.get("sys1_route")]
    conf = {g: {p: sum(1 for r in ok if r["llm_route"] == g and r.get("sys1_route") == p) for p in ROUTES} for g in ROUTES}
    llm_safe_only = [r for r in rows if r["llm_route"] == "safety" and r.get("sys1_route") != "safety"]       # every one, incl. Sys1 errors
    sys1_safe_only = [r for r in ok if r.get("sys1_route") == "safety" and r["llm_route"] != "safety"]
    lat = lambda k, R: {"p50": pct([r.get(k) for r in R], .5), "p95": pct([r.get(k) for r in R], .95), "n": sum(1 for r in R if r.get(k) is not None)}
    m = {"source": a.source, "consent_note": a.consent_note, "trace": os.path.abspath(a.trace), "since": a.since, "n": len(rows), "n_sys1_ok": len(ok),
         "agreement": len(agree) / len(ok) if ok else None, "agreement_ci": wilson(len(agree), len(ok)), "sys1_error_rate": len(err) / len(rows),
         "sys1_error_ci": wilson(len(err), len(rows)), "confusion_llm_x_sys1": conf, "llm_safety_sys1_not": len(llm_safe_only), "sys1_safety_llm_not": len(sys1_safe_only),
         "latency_ms": {"sys1_http": lat("sys1_ms", ok), "sys1_total_incl_parser": lat("sys1_total_ms", ok), "llm_parse_intent": lat("llm_ms", rows)},
         "models": sorted({str(r.get("model")) for r in rows}), "frozen_cands": sorted({str(r.get("frozen_cand")) for r in rows})}
    atomic_json(out / "s3_metrics.json", m)
    key = lambda r: r.get("text") or ("sha:" + r["q_sha"][:16])
    with open(out / "disagreements.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["llm_route", "sys1_route", "text_or_sha", "sys1_error", "p_safety", "p_emergency", "intents", "parser_safety_marker"])
        for r in sorted([r for r in rows if r["llm_route"] != r.get("sys1_route")], key=lambda r: (r["llm_route"], str(r.get("sys1_route")))):
            w.writerow([r["llm_route"], r.get("sys1_route"), key(r), r.get("sys1_error") or "", r.get("p_safety"), r.get("p_emergency"), r.get("intents"), r.get("parser_safety_marker")])
    with open(out / "safety_llm_only.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["text_or_sha", "sys1_route", "sys1_error", "p_safety", "p_emergency"])
        for r in llm_safe_only: w.writerow([key(r), r.get("sys1_route"), r.get("sys1_error") or "", r.get("p_safety"), r.get("p_emergency")])
    f3 = lambda x: "–" if x is None else f"{x:.3f}"; f0 = lambda x: "–" if x is None else f"{x:.0f}"
    L = [f"# S3 影子比對（{a.source}）\n",
         ("> **限制**：來源是合成情境（scenarios.json）。合成句的分布、措辭與真實使用者不同；一致率與安全差異只說明接線與明顯錯誤，**不能**當成真實流量上的準確率或安全漏判率。"
          if a.source == "synthetic" else f"> 來源：負責人提供、有同意紀錄的流量（{a.consent_note}）。"),
         "> 舊 router 的『安全』= production 規則（FIND_PRODUCT 且命中 SAFETY_KEYWORDS）；layer-1（含 119 緊急回覆）在 parse_intent 之前，不在本比對範圍。\n",
         f"- 列數 {len(rows)}（Sys1 成功 {len(ok)}，錯誤 {len(err)}）· trace `{a.trace}`",
         f"- 路由一致率（Sys1 成功列）：{f3(m['agreement'])} [95% CI {f3(m['agreement_ci'][0])}–{f3(m['agreement_ci'][1])}]",
         f"- Sys1 錯誤率：{f3(m['sys1_error_rate'])}（{len(err)}/{len(rows)}）",
         f"- **舊 router 判安全、P+Sys1 沒判：{len(llm_safe_only)} 句**（逐句列在 `safety_llm_only.csv`，需要人工逐一看）",
         f"- P+Sys1 判安全、舊 router 沒判：{len(sys1_safe_only)} 句",
         f"- 延遲 p50/p95：Sys1 HTTP {f0(m['latency_ms']['sys1_http']['p50'])}/{f0(m['latency_ms']['sys1_http']['p95'])} ms · 含 Parser {f0(m['latency_ms']['sys1_total_incl_parser']['p50'])}/{f0(m['latency_ms']['sys1_total_incl_parser']['p95'])} ms · 舊 LLM parse_intent {f0(m['latency_ms']['llm_parse_intent']['p50'])}/{f0(m['latency_ms']['llm_parse_intent']['p95'])} ms",
         "  （shadow 模式下 Sys1 在背景執行緒跑，與 LLM 呼叫同時；延遲可能被同機負載影響，canary 前請在 sys1 arm 再量一次）\n",
         "## 混淆矩陣（列＝舊 LLM router，欄＝P+Sys1）\n", "|llm＼sys1|" + "|".join(ROUTES) + "|", "|--|" + "--|" * len(ROUTES)]
    L += [f"|{g}|" + "|".join(str(conf[g][p]) for p in ROUTES) + "|" for g in ROUTES]
    L.append(f"\n不一致清單：`disagreements.csv`（{sum(1 for r in rows if r['llm_route'] != r.get('sys1_route'))} 列；trace_text=false 時只有 q_sha）。")
    (out / "SUMMARY.md").write_text("\n".join(L) + "\n", encoding="utf-8"); print("\n".join(L)); return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("drive"); d.add_argument("--base-url", required=True); d.add_argument("--scenarios", required=True); d.add_argument("--control", default=str(HERE / "control" / "sys1_dispatch.json"))

    d.add_argument("--timeout", type=float, default=120); d.add_argument("--sleep", type=float, default=0.0); d.add_argument("--settle", type=float, default=2.0)
    d.add_argument("--trace"); d.add_argument("--trace-text", action="store_true", help="記錄原句（只對合成或有同意的流量）")
    z = sp.add_parser("analyze"); z.add_argument("--trace", required=True); z.add_argument("--source", required=True, choices=["synthetic", "consented"])
    z.add_argument("--consent-note"); z.add_argument("--out", required=True); z.add_argument("--since", type=float, default=0)
    a = ap.parse_args(); return {"drive": cmd_drive, "analyze": cmd_analyze}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
