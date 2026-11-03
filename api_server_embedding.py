#!/opt/conda/envs/rocm_env/bin/python3
"""OpenAI-compatible BGE-M3 embedding service for Graph RAG."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sentence_transformers import SentenceTransformer

MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
PORT = int(os.getenv("EMBEDDING_PORT", "8002"))
DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu").strip()
MAX_CONCURRENT = max(
    1,
    int(os.getenv("EMBEDDING_MAX_CONCURRENT", "2")),
)
QUEUE_TIMEOUT = max(
    0.1,
    float(os.getenv("EMBEDDING_QUEUE_TIMEOUT_SECONDS", "10")),
)
ENCODE_SLOT = threading.BoundedSemaphore(MAX_CONCURRENT)

print(f"Loading embedding model {MODEL_NAME} on {DEVICE}...", flush=True)
embedding_model = SentenceTransformer(MODEL_NAME, device=DEVICE)
print("Embedding model ready", flush=True)


def generate_embedding(text: str, dimension: int = 1024) -> list:
    if not text:
        return [0.001] * dimension
    vector = embedding_model.encode(
        text,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()
    if len(vector) >= dimension:
        return vector[:dimension]
    return vector + [0.0] * (dimension - len(vector))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, data, status=200, headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json({"status": "ready", "model": MODEL_NAME})
        elif self.path == "/v1/models":
            self._send_json({
                "object": "list",
                "data": [{"id": "bge-m3", "object": "model"}],
            })
        else:
            self._send_json({"error": "not_found"}, status=404)

    def do_POST(self):
        if self.path != "/v1/embeddings":
            self._send_json({"error": "not_found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            user_input = body.get("input", "")
            text = user_input[0] if isinstance(user_input, list) else user_input
            if not isinstance(text, str) or not text.strip():
                self._send_json({"error": "input must contain text"}, status=400)
                return
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json({"error": f"invalid_request: {error}"}, status=400)
            return

        if not ENCODE_SLOT.acquire(timeout=QUEUE_TIMEOUT):
            self._send_json(
                {"error": "embedding_service_busy"},
                status=429,
                headers={"Retry-After": "1"},
            )
            return

        started_at = time.perf_counter()
        try:
            vector = generate_embedding(text.strip())
            self._send_json({
                "object": "list",
                "data": [{
                    "object": "embedding",
                    "embedding": vector,
                    "index": 0,
                }],
                "model": "bge-m3-cpu",
                "usage": {
                    "prompt_tokens": len(text),
                    "total_tokens": len(text),
                },
                "performance": {
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000,
                        2,
                    ),
                },
            })
        except Exception as error:
            self._send_json(
                {"error": f"embedding_failed: {type(error).__name__}"},
                status=500,
            )
        finally:
            ENCODE_SLOT.release()


if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((os.getenv("EMBEDDING_BIND_HOST", "127.0.0.1"), PORT), Handler)
    server.daemon_threads = True
    print(f"Embedding server listening on port {PORT}", flush=True)
    server.serve_forever()
