from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests
import json

UPSTREAM = "http://localhost:8000"

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))

        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        # =========================
        # 🧠 1. 修正 messages（防 prompt 外洩）
        # =========================
        messages = body.get("messages", [])

        # 保底：確保 role 正確
        clean_messages = []
        for m in messages:
            clean_messages.append({
                "role": m.get("role", "user"),
                "content": m.get("content", "")
            })

        body["messages"] = clean_messages

        # =========================
        # 🚀 2. 呼叫原 TAIDE server
        # =========================
        r = requests.post(
            UPSTREAM + self.path,
            json=body,
            timeout=300
        )

        data = r.json()

        # =========================
        # 🧹 3. 修正 prompt echo bug
        # =========================
        if "choices" in data:
            content = data["choices"][0]["message"]["content"]

            # 如果模型把 system prompt吐出來 → 清掉
            if "ROLE (角色定位)" in content:
                content = self._clean_prompt_echo(content)

            data["choices"][0]["message"]["content"] = content

        self._reply(data)

    def _clean_prompt_echo(self, text: str) -> str:
        """
        🔥 防止 TAIDE echo system prompt
        """
        markers = [
            "ROLE (角色定位)",
            "CONTEXT",
            "INPUT (輸入文本約束)",
            "OUTPUT"
        ]

        for m in markers:
            if m in text:
                text = text.split(m)[0]

        return text.strip()

    def _reply(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8003), Handler)
    print("🚀 TAIDE FIX LAYER running on 8003 → proxy 8000")
    server.serve_forever()