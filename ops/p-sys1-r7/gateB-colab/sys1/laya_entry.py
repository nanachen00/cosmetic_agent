#!/usr/bin/env python3
"""laya_entry — Laya（convaiinnovations/laya，pip `laya[serve]`）當 Sys1 候選時的服務入口：127.0.0.1:8230-8239。

laya-serve 本身就講 Kev／Jev 相同的 POST /v1/systemone，但直接拿來跑有三個問題：
  1. 沒有 GET /v1/models。gateb.py kev-collect 與 run_cand.py probe 會先打它（兩者在 FREEZE.json 裡，不能改）。
  2. 推論時間放在 X-Inference-Time-Ms／Server-Timing header，不在 JSON 的 latency_ms（gateb 的 server_ms／排隊估計讀這個欄位）。
  3. 預設綁 0.0.0.0:8000，正好是正式 TAIDE 閘道的埠。
這裡在同一個程序載入 laya.serve.create_app()，前面包一層 ASGI：補 /v1/models、把 header 的推論時間寫進 latency_ms、只綁 127.0.0.1。
其他請求原樣交給 Laya。Laya 的設定照它自己的環境變數（LAYA_MODELS、LAYA_DEVICE、LAYA_PRELOAD…）；這裡固定 LAYA_JEV_STRICT=1
（choice 回 choice/confidence/probabilities，noul 回 noul，和 Kev 同形），並清掉 LAYA_API_KEY（只綁本機，gateb 不送 Bearer）。

  python3 sys1/laya_entry.py --port 8231            # 由 ./run_vm.sh laya-serve / laya-contract / laya-s2 啟動
"""
import argparse, json, os, sys

PORTS = range(8230, 8240)
FORBIDDEN = {8000, 8001, 8002, 8003, 8010, 8200, 8201}


def _header(headers, name: bytes):
    for k, v in headers:
        if k.lower() == name: return v.decode("latin-1")
    return None


def inference_ms(headers):
    """X-Inference-Time-Ms，沒有就讀 Server-Timing 的 inference;dur=…；都沒有回 None。"""
    v = _header(headers, b"x-inference-time-ms")
    if v is None:
        st = _header(headers, b"server-timing") or ""
        for part in st.split(","):
            name, _, rest = part.strip().partition(";")
            if name == "inference" and "dur=" in rest: v = rest.split("dur=", 1)[1].split(";")[0]
    try: return float(v) if v is not None else None
    except ValueError: return None


class Adapter:
    """ASGI 包裝：GET /v1/models 自己回答；POST /v1/systemone 的 JSON 補 latency_ms；其餘原樣轉交。"""
    def __init__(self, app, models, info=None):
        self.app, self.models, self.info = app, list(models), dict(info or {})

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") == "GET" and scope.get("path") == "/v1/models":
            body = json.dumps({"models": [dict(self.info, name=m) for m in self.models]}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body}); return
        if not (scope.get("type") == "http" and scope.get("method") == "POST" and scope.get("path") == "/v1/systemone"):
            await self.app(scope, receive, send); return
        start, chunks = {}, []

        async def capture(msg):
            if msg["type"] == "http.response.start": start.update(msg); return
            if msg["type"] != "http.response.body": await send(msg); return
            chunks.append(msg.get("body", b""))
            if msg.get("more_body"): return
            body, headers = b"".join(chunks), list(start.get("headers", []))
            ms = inference_ms(headers)
            try: j = json.loads(body)
            except ValueError: j = None
            if isinstance(j, dict) and j.get("latency_ms") is None and ms is not None:
                j["latency_ms"] = ms; body = json.dumps(j, ensure_ascii=False).encode()
                headers = [(k, v) for k, v in headers if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
            await send(dict(start, headers=headers)); await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8231); ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    if a.port not in PORTS or a.port in FORBIDDEN: sys.exit("[REFUSED] Laya 只能開在 127.0.0.1:8230-8239")
    os.environ["LAYA_JEV_STRICT"] = "1"; os.environ.setdefault("USE_TF", "0")
    if os.environ.pop("LAYA_API_KEY", None): print("[laya_entry] LAYA_API_KEY 已忽略（只綁 127.0.0.1）", file=sys.stderr)
    import importlib.metadata as md, uvicorn
    from laya import serve as LS
    models = [m.strip() for m in os.environ.get("LAYA_MODELS", "").split(",") if m.strip()] or ["multilingual", "typed-decisions"]
    info = {"backend": "laya", "laya": md.version("laya"), "device": os.environ.get("LAYA_DEVICE", "auto")}
    print(f"[laya_entry] laya {info['laya']} models={models} device={info['device']} -> 127.0.0.1:{a.port}", flush=True)
    uvicorn.run(Adapter(LS.create_app(), models, info), host="127.0.0.1", port=a.port, log_level=a.log_level)   # Linux asyncio sets TCP_NODELAY itself
    return 0


if __name__ == "__main__":
    sys.exit(main())
