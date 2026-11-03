#!/opt/conda/envs/rocm_env/bin/python3
"""
TAIDE 簡易 OpenAI 相容 API Server
讓 intent_parser.py 透過 litellm 呼叫
"""
import json
import os
import threading
import time
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATH = "/var/lib/jenkins/tomod/taide-12b"
MAX_CONCURRENT_GENERATIONS = max(
    1,
    int(os.getenv("TAIDE_MAX_CONCURRENT_GENERATIONS", "1")),
)
QUEUE_TIMEOUT_SECONDS = max(
    0.1,
    float(os.getenv("TAIDE_QUEUE_TIMEOUT_SECONDS", "2")),
)
MAX_OUTPUT_TOKENS = max(
    1,
    int(os.getenv("TAIDE_MAX_OUTPUT_TOKENS", "600")),
)
_generation_slots = threading.BoundedSemaphore(
    MAX_CONCURRENT_GENERATIONS
)

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)

print("Loading model... (需要1-2分鐘)")
model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map="auto"
)
print("Model ready!")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 關掉預設 log

    def do_GET(self):
        if self.path in {"/healthz", "/readyz"}:
            self._send_json({"status": "ready", "model": "taide-12b"})
            return
        if self.path == "/v1/models":
            self._send_json({
                "object": "list",
                "data": [{"id": "taide-12b", "object": "model"}],
            })
            return
        self._send_json(
            {"error": {"type": "not_found", "message": "Not found"}},
            status_code=404,
        )


    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))

        # --- 1. 原本的聊天功能 (保留並整合) ---
        # --- 1. 原本的聊天功能 (白名單過濾版) ---
        if self.path == "/v1/chat/completions":
            acquired = _generation_slots.acquire(
                timeout=QUEUE_TIMEOUT_SECONDS
            )
            if not acquired:
                self._send_json(
                    {
                        "error": {
                            "type": "server_overloaded",
                            "message": (
                                "TAIDE is busy; retry after a short delay."
                            ),
                        }
                    },
                    status_code=429,
                    headers={"Retry-After": "2"},
                )
                return

            started_at = time.perf_counter()
            try:
                messages = body.get("messages", [])
                requested_tokens = int(body.get("max_tokens", 600))
                max_tokens = max(
                    1,
                    min(requested_tokens, MAX_OUTPUT_TOKENS),
                )
                temp = float(body.get("temperature", 0.1))

                text = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                inputs = tokenizer(
                    text,
                    return_tensors="pt",
                ).to(model.device)

                gen_kwargs = {
                    "input_ids": inputs.input_ids,
                    "attention_mask": inputs.attention_mask,
                    "max_new_tokens": max_tokens,
                    "do_sample": temp > 0,
                }
                if temp > 0:
                    gen_kwargs["temperature"] = temp

                with torch.inference_mode():
                    outputs = model.generate(**gen_kwargs)

                completion_tokens = max(
                    0,
                    int(outputs.shape[1] - inputs.input_ids.shape[1]),
                )
                response_text = tokenizer.decode(
                    outputs[0][inputs.input_ids.shape[1]:],
                    skip_special_tokens=True,
                )
                duration = time.perf_counter() - started_at

                result = {
                    "id": "chatcmpl-taide",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "taide-12b",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_text,
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": int(inputs.input_ids.shape[1]),
                        "completion_tokens": completion_tokens,
                        "total_tokens": int(inputs.input_ids.shape[1])
                        + completion_tokens,
                    },
                    "performance": {
                        "duration_ms": round(duration * 1000, 2),
                    },
                }
                self._send_json(result)
            finally:
                _generation_slots.release()

        # --- 2. 💡 新增：處理 Embedding 請求 (解決 404 問題) ---
        elif self.path == "/v1/embeddings":
            # 妳可以看到 GraphRAG 發過來的請求，但我們不跑模型
            # 直接回傳一串 1536 維的 0.0 (OpenAI 標準長度)
            mock_embedding = [0.0] * 1536 
            result = {
                "object": "list",
                "data": [{
                    "object": "embedding", 
                    "embedding": mock_embedding, 
                    "index": 0
                }],
                "model": "taide-12b",
                "usage": {"prompt_tokens": 0, "total_tokens": 0}
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            print("Intercepted Embedding request: Returned dummy vector.")

        # --- 3. 原本的 Models 列表功能 (保留) ---
        elif self.path == "/v1/models":
            result = {
                "object": "list",
                "data": [{"id": "taide-12b", "object": "model"}]
            }
            self._send_json(result)

        else:
            self.send_response(404)
            self.end_headers()

    # 💡 增加一個小幫手函數，讓代碼更乾淨
    def _send_json(
        self,
        data,
        status_code=200,
        headers=None,
    ):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    
# ... 以上是妳的模型載入與 Handler 類別 ...

if __name__ == "__main__":
    # 💡 確保這裡的類別名稱有對，且加入 allow_reuse_address 防止鎖死
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    
    # 💡 將原本的 8000 改成 8001
    server = ThreadingHTTPServer((os.getenv("TAIDE_BIND_HOST", "127.0.0.1"), 8000), Handler) 
    
    print(f"TAIDE server running on http://{server.server_address[0]}:8000")
    server.serve_forever()
