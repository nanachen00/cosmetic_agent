#!/usr/bin/env python3
"""Mock Shadow backend (127.0.0.1:8210-8219) that runs the REAL overlay: a patched shadow copy of the fixture orchestrator.
guest-session + POST /api/chat/message (production ChatRequest) -> {"stage","agent","llm_calls"}; fake LLM = fixture intent_parser.
Used by the S3 (s3_shadow.py drive) and T8 (rollback drill) tests only."""
import argparse, importlib, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8212); ap.add_argument("--shadow-root", required=True); ap.add_argument("--fail-5xx-every", type=int, default=0)
A = ap.parse_args()
if not 8210 <= A.port <= 8219: sys.exit("[REFUSED] mock backend port must be 8210-8219")
sys.path.insert(0, A.shadow_root)
ORCH = importlib.import_module("agents.root.orchestrator"); IP = importlib.import_module("tools.agent.intent_parser")
LOCK = threading.Lock(); CONV = {}; N = [0]


GUESTS = {}


class H(BaseHTTPRequestHandler):
    """Mimics cosmetic-agent: POST /api/auth/guest-session -> {"user_id"} + cookie blink_guest_session;
    POST /api/chat/message ChatRequest{user_id, conversation_id, message, flow} requires that cookie."""
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj, cookie=None):
        b = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        if cookie: self.send_header("Set-Cookie", f"blink_guest_session={cookie}; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send(200, {"ok": True, "llm_calls": len(IP.CALLS)})
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/auth/guest-session":
            import uuid
            tok, uid = uuid.uuid4().hex, "guest-" + uuid.uuid4().hex[:12]
            with LOCK: GUESTS[tok] = uid
            return self._send(200, {"user_id": uid}, cookie=tok)
        if self.path != "/api/chat/message": return self._send(403, {"detail": "Outside benchmark scope"})
        ck = dict(x.strip().split("=", 1) for x in (self.headers.get("Cookie") or "").split(";") if "=" in x)
        uid = GUESTS.get(ck.get("blink_guest_session"))
        if not uid or b.get("user_id") != uid: return self._send(401, {"detail": "no session"})
        if not isinstance(b.get("message"), str) or not b["message"]: return self._send(422, {"detail": "message required"})
        with LOCK:
            N[0] += 1
            if A.fail_5xx_every and N[0] % A.fail_5xx_every == 0: return self._send(503, {"error": "injected"})
            key = (uid, b.get("conversation_id") or "default")
            o = CONV.get(key)
            if o is None: o = CONV[key] = ORCH.Orchestrator(); o.user_id, o.conversation_id = key
            before = len(IP.CALLS)
            try: r = o._handle_new_input(b["message"])
            except Exception as e: return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(200, {"stage": r.get("stage"), "agent": r.get("agent"), "llm_calls": len(IP.CALLS) - before})


ThreadingHTTPServer(("127.0.0.1", A.port), H).serve_forever()
