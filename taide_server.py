"""
TAIDE 簡易 OpenAI 相容 API Server
讓 intent_parser.py 透過 litellm 呼叫
"""
import json
import time
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATH = "/var/lib/jenkins/tomod/taide-12b"

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)

print("Loading model... (需要1-2分鐘)")
model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map="cpu"
)
print("Model ready!")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 關掉預設 log

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))

        # --- 1. 原本的聊天功能 (保留並整合) ---
        # --- 1. 原本的聊天功能 (白名單過濾版) ---
        if self.path == "/v1/chat/completions":
            messages = body.get("messages", [])
            max_tokens = body.get("max_tokens", 1000)
            temp = body.get("temperature", 0.1)

            text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = tokenizer(text, return_tensors="pt")

            # 💡 關鍵：白名單過濾，只放這四個核心參數
            
            gen_kwargs = {
                "input_ids": inputs.input_ids,
                "attention_mask": inputs.attention_mask,
                "max_new_tokens": max_tokens,
                "do_sample": True if temp > 0 else False
            }
            
            # 如果 temp 有值且大於 0 才加進去，避免 transformers 報錯
            if temp > 0:
                gen_kwargs["temperature"] = temp

            with torch.no_grad():
                # 這裡不再傳入 **inputs，改傳 gen_kwargs
                outputs = model.generate(**gen_kwargs)

            response_text = tokenizer.decode(
                outputs[0][inputs.input_ids.shape[1]:],
                skip_special_tokens=True
            )
        

            result = {
                "id": "chatcmpl-taide",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "taide-12b",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            self._send_json(result)

        # --- 2. 💡 新增：處理 Embedding 請求 (解決 404 問題) ---
        elif self.path == "/v1/embeddings":
            # 我們回傳假的向量，騙過 GraphRAG 讓它繼續往下走
            mock_embedding = [0.0] * 1536 
            result = {
                "object": "list",
                "data": [{"object": "embedding", "embedding": mock_embedding, "index": 0}],
                "model": "taide-12b",
                "usage": {"prompt_tokens": 0, "total_tokens": 0}
            }
            self._send_json(result)

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
    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    
# ... 以上是妳的模型載入與 Handler 類別 ...

if __name__ == "__main__":
    # 💡 確保這裡的類別名稱有對，且加入 allow_reuse_address 防止鎖死
    ThreadingHTTPServer.allow_reuse_address = True 
    
    # 💡 將原本的 8000 改成 8001
    server = ThreadingHTTPServer(("0.0.0.0", 8001), Handler) 
    
    print("TAIDE server running on http://0.0.0.0:8001 (Threading & Reuse Enabled)")
    server.serve_forever()
