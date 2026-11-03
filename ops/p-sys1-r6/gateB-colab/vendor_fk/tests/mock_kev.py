#!/usr/bin/env python3
"""Fake Kev for tests (127.0.0.1 only).  Rule-based; NOT a model.
Usage: mock_kev.py --port 8239 [--delay-ms 0] [--per-q-ms 0] [--fail-every 0] [--invert-noul] [--err-rate 0.0] [--workers 0]
Answers the legacy 3 questions (route/needs_clarification/safety_sensitive) AND arbitrary gate questions by name/instructions heuristics.
--err-rate: deterministically (hash of text+question) flips a fraction of answers, to exercise error-propagation counters.
--workers N: serialise inference through N slots (to see queueing in the concurrency ladder)."""
import argparse, hashlib, json, re, sys, time, threading, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "payload"))
import kev_slot_parser as P
ROUTES = ["product", "knowledge", "skin_tracking", "conversation", "safety", "clarify"]
N = [0]; SEM = [None]
SAFE = re.compile(r"孕|哺乳|過敏|刺激|一起用|可以用|能不能|安全|喘|腫|禁忌|副作用")
NEG_SAFE = re.compile(r"(?:沒有|沒|並沒有)\s*過敏")
KW = {"find_product": r"推薦|想找|想買|預算|有沒有.*(?:乳|霜|精華|防曬|洗面|水|面膜)", "price_comparison": r"划算|價格|CP值|比較.*價",
      "knowledge": r"是什麼|功效|作用|原理|為什麼|適合|差別|原因|可以用|一起用|安全|過敏", "record": r"記錄|記起來|記下", "chitchat": r"^(?:你好|哈囉|謝謝|晚安|辛苦|你是誰|今天天氣)|謝謝",
      "unclear": r"^(?:我想|那個呢|幫我弄一下|這個可以嗎|我想要那個|還有嗎)$"}
CONC = {"保濕": "保濕|補水|乾燥|脫皮", "抗痘": "痘", "控油": "控油|出油|油", "美白": "美白", "舒緩": "舒緩|鎮靜|紅", "抗皺": "抗皺", "毛孔": "毛孔"}


def flip(seed, rate):
    return rate > 0 and int(hashlib.md5(seed.encode()).hexdigest(), 16) % 1000 < rate * 1000


def choice_ans(options, pick, conf=0.85):
    rest = [o for o in options if o != pick]
    pr = {o: (1 - conf) / max(1, len(rest)) for o in rest}; pr[pick] = conf
    return {"choice": pick, "probabilities": pr, "confidence": conf}


def answer(text, name, q, A):
    typ = q.get("type") or ("choice" if name in ("route", "skin_type", "product_category", "concern", "texture") else "noul"); instr = q.get("instructions", ""); crit = q.get("criteria", {})
    bad = flip(text + name, A.err_rate)
    if typ == "noul":
        if name == "safety" or name == "safety_sensitive":
            p = 0.9 if (SAFE.search(text) and not NEG_SAFE.search(text)) else 0.05
        elif name.startswith("i_"):
            lab = name[2:]; p = 0.9 if re.search(KW.get(lab, "$^"), text) else 0.05
            if lab == "knowledge" and re.search(r"可以用|一起用|安全|過敏", text) and not re.search(r"是什麼|功效|作用|原理|為什麼", text): p = 0.05 if lab != "knowledge" else 0.9
        elif name.startswith("avoid_"):
            m = re.search(r"「(.+?)」", instr); ing = m.group(1) if m else "@@"
            p = 0.9 if re.search(r"(?:不要|避免使用|避開|不含|不想用)\s*(?:含|用)?" + re.escape(ing), text) else 0.05
        elif name == "needs_clarification":
            p = 0.9 if P.deterministic_route_hint(P.parse(text)) is None else 0.05
        else: p = 0.5
        if bad: p = 1 - p
        return {"noul": p if not A.invert_noul else 1 - p}
    opts = list(crit)
    if name in ("skin_type", "product_category", "texture") or (name == "concern" and "無明確訴求" not in crit):   # legacy slot probe (fp_load / dispatch overlay)
        val = {"skin_type": P.parse(text).skin_type or "unknown", "product_category": "乳液" if "乳液" in text else "不明",
               "concern": "保濕" if "保濕" in text else "不明", "texture": "清爽" if "清爽" in text else "不明"}[name]
        return {"choice": val, "confidence": 0.8}
    if name == "concern":
        pick = next((c for c, pat in CONC.items() if re.search(pat, text)), "無明確訴求")
        if bad: pick = "無明確訴求" if pick != "無明確訴求" else "保濕"
    elif name == "intent_choice":
        pick = next((l for l in KW if re.search(KW[l], text)), "unclear")
        if bad: pick = "chitchat"
    elif name.startswith("role_"):
        sp = (re.search(r"「(.+?)」", instr).group(1) if re.search(r"「(.+?)」", instr) else ""); pick = "other" if re.search(r"ml|歲|公克", sp) else ("target" if re.search(r"左右|大約|約", text) else "upper" if re.search(r"以內|最高|不超過|最多|上限", text) else "upper")
        if bad: pick = "target" if pick != "target" else "upper"
    elif name == "route":
        pick = P.deterministic_route_hint(P.parse(text)) or "clarify"
    else:
        pickp = P.parse(text); pick = (getattr(pickp, name, None) or opts[0]) if name in ("skin_type",) else opts[0]
    if pick not in opts: pick = opts[0] if opts else "unknown"
    return choice_ans(opts, pick)


class S(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    disable_nagle_algorithm = True
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send(200, {"data": [{"id": "kev-latest"}]})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        N[0] += 1
        if A.fail_every and N[0] % A.fail_every == 0: return self._send(500, {"error": "injected"})
        qs = body.get("questions") or {}
        if not qs: return self._send(422, {"error": "no questions"})
        if any(("type" in q) and q["type"] not in ("choice", "noul") for q in qs.values()): return self._send(422, {"error": "bad type"})
        text = body.get("state", "")
        if SEM[0]: SEM[0].acquire()
        try:
            t0 = time.perf_counter(); time.sleep((A.delay_ms + A.per_q_ms * len(qs)) / 1000)
            a = {n: answer(text, n, q, A) for n, q in qs.items()}
            if "route" in qs:                                        # legacy 3-question probe keeps working
                r = a["route"]["choice"]; a["route"] = choice_ans(ROUTES, r if r in ROUTES else "clarify", 0.9)
            self._send(200, {"answers": a, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
        finally:
            if SEM[0]: SEM[0].release()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, required=True); ap.add_argument("--delay-ms", type=float, default=0); ap.add_argument("--per-q-ms", type=float, default=0)
    ap.add_argument("--fail-every", type=int, default=0); ap.add_argument("--invert-noul", action="store_true"); ap.add_argument("--err-rate", type=float, default=0.0); ap.add_argument("--workers", type=int, default=0)
    A = ap.parse_args()
    if A.workers: SEM[0] = threading.Semaphore(A.workers)
    S(("127.0.0.1", A.port), H).serve_forever()
