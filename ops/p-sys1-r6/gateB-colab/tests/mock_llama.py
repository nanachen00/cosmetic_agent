#!/usr/bin/env python3
"""Fake llama-server for selftests (127.0.0.1 only). Rule-based, NOT a model. Implements /health, /props, /v1/chat/completions
with llama.cpp-style `timings`. Output shape follows the schema named in the system prompt (legacy / ext / min). In prompt-only
mode it sometimes wraps the JSON in ``` fences and, for ~5% of inputs (hash-based), returns broken JSON to exercise parse-fail counting.
Usage: mock_llama.py --port 8359 [--model-path /x/gemma-4-E2B-it-Q8_0.gguf] [--ms-per-tok 0] [--slots 4]"""
import argparse, hashlib, json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAFE = re.compile(r"孕|哺乳|過敏|刺激|一起用|安全|喘|腫|紅疹"); NEG = re.compile(r"沒有過敏")
ING = {"hyaluronic_acid": "玻尿酸", "ceramide": "神經醯胺", "niacinamide": "菸鹼醯胺", "retinol": "A酸", "salicylic_acid": "水楊酸", "aha": "果酸"}
CN = {"三百": 300, "五百": 500, "八百": 800, "一千二": 1200, "一千五": 1500, "一千": 1000, "兩千": 2000, "三千": 3000}


def h(s): return int(hashlib.md5(s.encode()).hexdigest(), 16)


def analyse(t):
    ints = []
    if re.search(r"推薦|想找|想買|預算|商品", t) and not t.startswith("不是要找"): ints.append("find_product")
    if re.search(r"划算|價格|CP值", t): ints.append("price_comparison")
    if re.search(r"是什麼|功效|作用|原理|為什麼|差別|原因|可以用|一起用|安全|過敏|刺激|喘|腫|紅疹|擦", t): ints.append("knowledge")
    if re.search(r"記錄|記起來", t): ints.append("record")
    if re.search(r"^(你好|哈囉|謝謝|晚安|辛苦|你是誰|今天天氣)", t): ints.append("chitchat")
    if not ints: ints = ["unclear"]
    v = None; role = "none"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]", t) or re.search(r"(\d{3,5})\s*(?:元|塊| 元|以內|左右)", t) or re.search(r"NT\$(\d+)", t)
    if m: v = int(float(m.group(1)) * (1000 if re.search(r"[kK]", m.group(0)) else 1))
    else:
        for k, x in CN.items():
            if k in t: v = x; break
    if v: role = "target" if re.search(r"左右|大約", t) else "upper"
    avoid = [k for k, z in ING.items() if re.search(r"(不要|避免使用|避開)" + re.escape(z), t)]
    con = [c for c in ["保濕", "抗痘", "控油", "美白", "舒緩", "抗皺", "毛孔"] if c in t]
    return {"intents": ints[:3], "safety_flag": bool(SAFE.search(t) and not NEG.search(t)), "emergency_flag": bool(re.search(r"喘|呼吸困難|腫", t)),
            "concern": con[:1], "budget_value": v, "budget_role": role, "avoid_ingredients": avoid}


def render(sysmsg, t):
    a = analyse(t)
    if "intent_type" in sysmsg:
        it = {"find_product": "find_product", "knowledge": "knowledge_query", "price_comparison": "price_comparison"}.get(a["intents"][0], "unclear")
        return {"intent_type": it, "skin_type": "unknown", "concern": a["concern"], "product_category": "不明", "texture_preference": "不明",
                "budget_range": str(a["budget_value"]) if a["budget_value"] else None, "brand_preference": [], "urgency_flag": False, "query_expansion": []}
    if "query_expansion" in sysmsg:
        return {"intents": a["intents"], "safety_flag": a["safety_flag"], "emergency_flag": a["emergency_flag"], "skin_type": "unknown", "concern": a["concern"],
                "product_category": "不明", "texture_preference": "不明", "budget_value": a["budget_value"], "budget_role": a["budget_role"],
                "avoid_ingredients": a["avoid_ingredients"], "brand_preference": [], "urgency_flag": False, "query_expansion": [t[:6]]}
    return a


class S(ThreadingHTTPServer):
    daemon_threads = True; request_queue_size = 128


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        try: self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError): pass        # client was killed on purpose (interrupt test)
    def do_GET(self):
        if self.path == "/health": return self._send(200, {"status": "ok"})
        if self.path == "/props": return self._send(200, {"model_path": A.model_path, "total_slots": A.slots, "build_info": "mock", "chat_template": "mock-gemma4",
                                                          "default_generation_settings": {"n_ctx": 4096}})
        return self._send(404, {"error": "nf"})
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = b["messages"]; sysm = msgs[0]["content"]; t = msgs[-1]["content"]
        SEM.acquire()
        try:
            t0 = time.perf_counter(); obj = render(sysm, t); s = json.dumps(obj, ensure_ascii=False)
            if "response_format" not in b:
                if h(t + "fence") % 3 == 0: s = "```json\n" + s + "\n```"
                if h(t + "broken") % 20 == 0: s = s[: len(s) // 2]
            ntok = max(1, len(s) // 2); time.sleep(A.ms_per_tok * ntok / 1000)
            pms = 5.0; dms = (time.perf_counter() - t0) * 1000
            msg = {"role": "assistant", "content": s}
            if A.leak_thinking and "response_format" in b: msg["reasoning_content"] = "Thinking Process: ..."
            self._send(200, {"choices": [{"message": msg, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": len(sysm) // 2 + len(t), "completion_tokens": ntok},
                             "timings": {"cache_n": len(sysm) // 2, "prompt_n": len(t), "prompt_ms": pms, "predicted_n": ntok, "predicted_ms": dms}})
        finally:
            SEM.release()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, required=True); ap.add_argument("--model-path", default="/models/gemma-4-E2B-it-Q8_0.gguf")
    ap.add_argument("--ms-per-tok", type=float, default=0.0); ap.add_argument("--slots", type=int, default=4); ap.add_argument("--leak-thinking", action="store_true")
    A = ap.parse_args(); SEM = threading.Semaphore(A.slots)
    S(("127.0.0.1", A.port), H).serve_forever()
