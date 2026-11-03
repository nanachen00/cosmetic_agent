#!/usr/bin/env python3
"""llmclient — OpenAI-compatible client for the local llama-server that serves Gemma in Gate B. Stdlib only.

SAFETY: only http://127.0.0.1:8350-8359 is accepted. 8000/8001/8002/8003/8010 (server-side vLLM/production ports),
8200-8299 (vLLM metrics / shadow backends) and 8230-8239 (Kev) are never valid LLM targets here."""
from __future__ import annotations
import http.client, json, socket, threading, time, urllib.parse

ALLOWED_PORTS = range(8350, 8360)


def check_base(url: str):
    u = urllib.parse.urlparse(url)
    if u.scheme != "http" or u.hostname != "127.0.0.1" or u.port not in ALLOWED_PORTS or u.path not in ("", "/"):
        raise SystemExit(f"[REFUSED] Gemma target must be http://127.0.0.1:8350-8359 (local llama-server only), got {url!r}")
    return u.hostname, u.port


class _NoDelay(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        try: self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError: pass


class LLMClient:
    def __init__(self, base_url="http://127.0.0.1:8350", timeout=180.0):
        self.host, self.port = check_base(base_url); self.timeout = timeout; self._tls = threading.local()

    def _conn(self):
        c = getattr(self._tls, "c", None)
        if c is None: c = self._tls.c = _NoDelay(self.host, self.port, timeout=self.timeout)
        return c

    def _reset(self):
        c = getattr(self._tls, "c", None)
        try:
            if c: c.close()
        finally: self._tls.c = None

    def request(self, method, path, body=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        t0 = time.perf_counter()
        for attempt in (0, 1):
            try:
                c = self._conn(); c.request(method, path, raw, {"Content-Type": "application/json"} if raw else {})
                r = c.getresponse(); data = r.read(); wall = (time.perf_counter() - t0) * 1000
                try: j = json.loads(data)
                except Exception: j = None
                return {"status": r.status, "wall_ms": wall, "json": j, "text": None if j is not None else data[:300].decode("utf-8", "replace")}
            except (http.client.HTTPException, OSError) as e:
                self._reset()
                if attempt == 1:
                    return {"status": 0, "wall_ms": (time.perf_counter() - t0) * 1000, "json": None, "text": f"{type(e).__name__}: {e}"}

    def health(self): return self.request("GET", "/health")
    def props(self): return self.request("GET", "/props")

    def chat(self, body: dict) -> dict:
        """-> {status, wall_ms, content, reasoning, finish, usage, timings, server_ms, queue_est_ms, error}"""
        r = self.request("POST", "/v1/chat/completions", body)
        j = r["json"] if isinstance(r["json"], dict) else {}
        out = {"status": r["status"], "wall_ms": r["wall_ms"], "content": None, "reasoning": None, "finish": None,
               "usage": j.get("usage"), "timings": j.get("timings"), "server_ms": None, "queue_est_ms": None,
               "error": None if r["status"] == 200 else (j.get("error") or r["text"])}
        try:
            ch = j["choices"][0]; msg = ch.get("message") or {}
            out["content"] = msg.get("content"); out["reasoning"] = msg.get("reasoning_content"); out["finish"] = ch.get("finish_reason")
        except Exception:
            pass
        t = out["timings"] or {}
        if t.get("prompt_ms") is not None and t.get("predicted_ms") is not None:
            out["server_ms"] = t["prompt_ms"] + t["predicted_ms"]; out["queue_est_ms"] = max(0.0, r["wall_ms"] - out["server_ms"])
        return out
