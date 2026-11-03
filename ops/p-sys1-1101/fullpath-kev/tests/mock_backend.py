#!/usr/bin/env python3
"""Fake Shadow backend for load-runner tests (127.0.0.1:8211).  Emulates: arm=off -> 'LLM' delay; arm=kev_* -> calls mock Kev and appends a trace row like the overlay."""
import argparse, hashlib, json, os, time, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8211); ap.add_argument("--control", required=True); ap.add_argument("--kev", default="http://127.0.0.1:8239"); ap.add_argument("--llm-s", type=float, default=0.4)
A = ap.parse_args(); LOCK = threading.Lock(); SEM = threading.Semaphore(4)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); text = b["message"]; ctl = json.load(open(A.control))
        t0 = time.perf_counter()
        if ctl["arm"] == "off":
            with SEM: time.sleep(A.llm_s)
        else:
            body = json.dumps({"state": text, "model": "kev-latest", "questions": {"route": {}, "needs_clarification": {}, "safety_sensitive": {}}}).encode()
            t = time.perf_counter(); r = json.loads(urllib.request.urlopen(urllib.request.Request(A.kev + "/v1/systemone", data=body, headers={"Content-Type": "application/json"})).read())
            km = (time.perf_counter() - t) * 1000; ch = r["answers"]["route"]["choice"]
            with LOCK, open(ctl["trace"], "a") as f: f.write(json.dumps({"ts": time.time(), "arm": ctl["arm"], "q_sha": hashlib.sha256(text.encode()).hexdigest(), "kev_ms": km, "kev_server_ms": r.get("latency_ms"), "route": ch, "parser_ms": 0.1}) + "\n")
            time.sleep(0.05)
        out = json.dumps({"reply": "ok"}).encode(); self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
ThreadingHTTPServer(("127.0.0.1", A.port), H).serve_forever()
