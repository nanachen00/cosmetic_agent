from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
import json
import os

CHAT_URL = "http://localhost:8000"
EMBED_URL = "http://localhost:8002"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)

        if self.path == "/v1/chat/completions":
            r = requests.post(CHAT_URL + self.path, data=body)
            self._reply(r)

        elif self.path == "/v1/embeddings":
            r = requests.post(EMBED_URL + self.path, data=body)
            self._reply(r)

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path in {"/healthz", "/readyz"}:
            payload = json.dumps({
                "status": "ready",
                "service": "taide-router",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/v1/models":
            r = requests.get(CHAT_URL + "/v1/models", timeout=2)
            self._reply(r)
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, r):
        self.send_response(r.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(r.content)


if __name__ == "__main__":
    server = ThreadingHTTPServer((os.getenv("TAIDE_ROUTER_BIND_HOST", "127.0.0.1"), 8001), Handler)
    print("🚀 Router running on 8001 → chat:8000 / embed:8002")
    server.serve_forever()