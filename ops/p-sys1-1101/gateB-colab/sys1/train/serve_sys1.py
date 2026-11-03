#!/usr/bin/env python3
"""serve_sys1 — 把 train_head 的 checkpoint 以 System One 契約（handoff §3）服務出來。只綁 127.0.0.1:8232–8239。

  python3 sys1/train/serve_sys1.py --ckpt ckpt/mmbert-ft-s13 --name mmbert-ft --port 8232 [--device auto|cpu|cuda] [--threads 4]

  GET  /v1/models    → {"data":[{"id": <name>}]}
  POST /v1/systemone → {"answers":{name:{"noul":p} | {"choice":k,"probabilities":{k:p},"confidence":p}},"latency_ms":x,"truncated":n}
  GET  /v1/stats     → 請求數、錯誤數、截斷數（S3/T8 監控用）

- 題目依名稱＋逐字內容對應到頭（catalog.plan_questions）；不認得或內容不同 → 整個請求 422，列出原因（不部分作答、不亂答）。
- model 欄位必須等於 --name（防止把請求送錯模型）；不同 → 400。
- 機率 = activation(logit / T)，T 是 train_head 在 cal 上擬合的 temperature。noul 高 = 是。
- 題目不進模型 context：輸入只有句子（與 pair 頭的金額片段／成分名），所以 ctx 只受句長影響；超過 max_len 的句子會截斷並計數回報。
"""
from __future__ import annotations
import argparse, json, math, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import catalog as C   # noqa: E402

ALLOWED = range(8232, 8240)


def check_port(host, port):
    if host != "127.0.0.1" or port not in ALLOWED:
        raise SystemExit(f"[REFUSED] serve_sys1 只能綁 127.0.0.1:8232-8239，收到 {host}:{port}")


def _sig(x): return 1 / (1 + math.exp(-max(-60, min(60, x))))


def _softmax(z, T):
    m = max(z); e = [math.exp((v - m) / T) for v in z]; s = sum(e); return [v / s for v in e]


def load(ckpt, device="auto"):
    meta = json.load(open(os.path.join(ckpt, "meta.json"), encoding="utf-8"))
    if meta.get("catalogue_signature") != C.K.catalogue_signature(): raise SystemExit("[serve] checkpoint 的題庫簽章與目前 kevlib 不同，拒絕服務")
    if meta["backend"] == "np-tiny":
        import model_np as M; return M.NpBackend.load(ckpt, meta), meta
    import model_hf as M; return M.HfBackend.load(ckpt, meta, device), meta


class Sys1:
    def __init__(self, be, meta, name):
        self.be, self.meta, self.name, self.T = be, meta, name, meta["temperature"]
        self.lock = threading.Lock(); self.slock = threading.Lock(); self.stats = {"requests": 0, "ok": 0, "rejected_422": 0, "errors_500": 0, "truncated": 0, "started": time.strftime("%F %T")}

    def bump(self, k, n=1):
        with self.slock: self.stats[k] += n

    def answer(self, text, qs):
        plan, errs = C.plan_questions(qs)
        if errs: return 422, {"error": "unsupported_questions", "details": errs}
        pairs = [(h, arg) for _, h, arg in plan if h in C.PAIR_HEADS]
        with self.lock: out, info = self.be.logits([(text, pairs)])
        l = out[0]; T = self.T; ans = {}; k = 0
        for name, h, arg in plan:
            if h in ("safety", "emergency"): ans[name] = {"noul": _sig(l[h] / T[h])}
            elif h == "intent": ans[name] = {"noul": _sig(l["intent"][arg] / T["intent"])}
            elif h == "concern":
                p = _softmax(l["concern"], T["concern"]); i = max(range(len(p)), key=p.__getitem__)
                ans[name] = {"choice": C.CONCERN_LABELS[i], "probabilities": dict(zip(C.CONCERN_LABELS, p)), "confidence": p[i]}
            elif h == "role":
                p = _softmax(l["pairs"][k], T["role"]); k += 1; i = max(range(len(p)), key=p.__getitem__)
                ans[name] = {"choice": C.ROLE_LABELS[i], "probabilities": dict(zip(C.ROLE_LABELS, p)), "confidence": p[i]}
            elif h == "avoid":
                ans[name] = {"noul": _sig(l["pairs"][k][0] / T["avoid"])}; k += 1
        return 200, {"answers": ans, "truncated": info["truncated"], "max_tokens": info["max_tokens"]}


def make_handler(S):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"; disable_nagle_algorithm = True
        def log_message(self, *a): pass
        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path == "/v1/models": return self._send(200, {"data": [{"id": S.name, "base": S.meta["base"], "backend": S.meta["backend"], "seed": S.meta["seed"]}]})
            if self.path == "/v1/stats": return self._send(200, S.stats)
            self._send(404, {"error": "not found"})
        def do_POST(self):
            t0 = time.perf_counter(); S.bump("requests")
            if self.path != "/v1/systemone": return self._send(404, {"error": "not found"})
            try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except Exception: S.bump("rejected_422"); return self._send(400, {"error": "bad json"})
            if body.get("model") != S.name: S.bump("rejected_422"); return self._send(400, {"error": f"model must be {S.name!r}, got {body.get('model')!r}"})
            text = body.get("state")
            if not isinstance(text, str) or not text.strip(): S.bump("rejected_422"); return self._send(422, {"error": "state must be a non-empty string"})
            try: code, obj = S.answer(text, body.get("questions"))
            except Exception as e: S.bump("errors_500"); return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            if code == 200:
                S.bump("ok"); S.bump("truncated", int(obj["truncated"] > 0)); obj["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            else: S.bump("rejected_422")
            self._send(code, obj)
    return H


class Srv(ThreadingHTTPServer):
    daemon_threads = True; request_queue_size = 256
    allow_reuse_address = os.name != "nt"     # Windows SO_REUSEADDR lets a second server bind a busy port; Linux unchanged


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--ckpt", required=True); ap.add_argument("--name", required=True); ap.add_argument("--port", type=int, default=8232)
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--device", default="auto"); ap.add_argument("--threads", type=int, default=0)
    a = ap.parse_args(); check_port(a.host, a.port)
    if a.threads:
        try: import torch; torch.set_num_threads(a.threads)
        except ImportError: pass
    be, meta = load(a.ckpt, a.device); S = Sys1(be, meta, a.name)
    be.logits([("暖機", [("role", "100元"), ("avoid", "A酸")])])                         # warm-up (first-call latency not counted)
    print(f"[serve] {a.name} ({meta['base']}, {meta['backend']}, seed {meta['seed']}) on http://{a.host}:{a.port}", flush=True)
    Srv((a.host, a.port), make_handler(S)).serve_forever()


if __name__ == "__main__":
    main()
