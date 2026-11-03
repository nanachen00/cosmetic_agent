"""
TAIDE Skin Agent 專用 OpenAI 相容 API Server
Port: 8002
"""

import json
import time
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATH = "/var/lib/jenkins/tomod/taide-12b"

print("Torch version:", torch.__version__)
print("HIP version:", torch.version.hip)
print("CUDA available:", torch.cuda.is_available())
print("CUDA device count:", torch.cuda.device_count())

if torch.cuda.is_available():
    print("GPU name:", torch.cuda.get_device_name(0))

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)

print("Loading model...")

model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
    device_map="auto"
)

model.eval()

print("Model ready!")

try:
    print("Model device:", model.device)
except Exception:
    print("Model device: device_map auto, model may be sharded")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body or b"{}")

            if self.path == "/v1/chat/completions":
                messages = body.get("messages", [])
                max_tokens = min(body.get("max_tokens", 300), 500)
                temp = body.get("temperature", 0.1)

                text = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True
                )

                inputs = tokenizer(text, return_tensors="pt")

                target_device = next(model.parameters()).device
                inputs = {
                    k: v.to(target_device)
                    for k, v in inputs.items()
                }

                gen_kwargs = {
                    "input_ids": inputs["input_ids"],
                    "attention_mask": inputs["attention_mask"],
                    "max_new_tokens": max_tokens,
                    "do_sample": True if temp > 0 else False,
                    "pad_token_id": tokenizer.eos_token_id
                }

                if temp > 0:
                    gen_kwargs["temperature"] = temp

                start_time = time.time()

                with torch.no_grad():
                    outputs = model.generate(**gen_kwargs)

                elapsed = round(time.time() - start_time, 2)

                response_text = tokenizer.decode(
                    outputs[0][inputs["input_ids"].shape[1]:],
                    skip_special_tokens=True
                )

                result = {
                    "id": "chatcmpl-taide-skin",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "taide-12b-skin",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_text
                            },
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0
                    },
                    "debug": {
                        "generation_seconds": elapsed,
                        "device": str(target_device)
                    }
                }

                self._send_json(result)

            elif self.path == "/v1/models":
                result = {
                    "object": "list",
                    "data": [
                        {
                            "id": "taide-12b-skin",
                            "object": "model"
                        }
                    ]
                }
                self._send_json(result)

            elif self.path == "/v1/embeddings":
                mock_embedding = [0.0] * 1536
                result = {
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "embedding": mock_embedding,
                            "index": 0
                        }
                    ],
                    "model": "taide-12b-skin",
                    "usage": {
                        "prompt_tokens": 0,
                        "total_tokens": 0
                    }
                }
                self._send_json(result)

            else:
                self.send_response(404)
                self.end_headers()

        except Exception as e:
            print("SERVER ERROR:", repr(e))
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            )

    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        )


if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True

    server = ThreadingHTTPServer(
        ("0.0.0.0", 8002),
        Handler
    )

    print("TAIDE Skin server running on http://0.0.0.0:8002")
    server.serve_forever()