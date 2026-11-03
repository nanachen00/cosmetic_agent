"""TAIDE :8000 compatibility gateway in front of vLLM (replaces the HF Transformers server).

Added 2026-10-21. It keeps the exact contract the backend relied on from
taide_server_graphrag.py, while generation runs on vLLM (continuous batching):

  GET  /healthz, /readyz          -> {"status": "ready", "model": "taide-12b"} only when vLLM is healthy
  GET  /v1/models                 -> models of every configured upstream
  POST /v1/chat/completions       -> proxied to vLLM (streaming supported)
       * max_tokens capped at TAIDE_MAX_OUTPUT_TOKENS (default 600), default 600
       * temperature defaults to 0.1 when the caller omits it (old server behaviour)
       * "model" selects the upstream: taide-12b (prod) or taide-12b-2602 (A/B)
  POST /v1/embeddings             -> forwarded to the BGE-M3 server on :8002
                                     (the old server returned 1536 zeros; real vectors are safer)

Run (vLLM env already has fastapi/uvicorn/httpx):
  python taide_vllm_gateway.py --port 8000
Upstreams are configured with TAIDE_UPSTREAMS, e.g.
  TAIDE_UPSTREAMS="taide-12b=http://127.0.0.1:8200,taide-12b-2602=http://127.0.0.1:8201"
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

DEFAULT_MODEL = os.getenv("TAIDE_DEFAULT_MODEL", "taide-12b")
MAX_OUTPUT_TOKENS = max(1, int(os.getenv("TAIDE_MAX_OUTPUT_TOKENS", "600")))
DEFAULT_TEMPERATURE = float(os.getenv("TAIDE_DEFAULT_TEMPERATURE", "0.1"))
EMBEDDING_URL = os.getenv("TAIDE_EMBEDDING_UPSTREAM", "http://127.0.0.1:8002").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("TAIDE_GATEWAY_TIMEOUT_SECONDS", "180"))


def _parse_upstreams(raw: str) -> dict[str, str]:
    upstreams: dict[str, str] = {}
    for item in raw.split(","):
        if "=" in item:
            name, url = item.split("=", 1)
            if name.strip() and url.strip():
                upstreams[name.strip()] = url.strip().rstrip("/")
    return upstreams


UPSTREAMS = _parse_upstreams(
    os.getenv("TAIDE_UPSTREAMS", f"{DEFAULT_MODEL}=http://127.0.0.1:8200")
)
if DEFAULT_MODEL not in UPSTREAMS:
    raise SystemExit(f"TAIDE_UPSTREAMS must include the default model {DEFAULT_MODEL}")

client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=5.0), limits=httpx.Limits(max_connections=256))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    await client.aclose()


app = FastAPI(title="TAIDE vLLM gateway", docs_url=None, redoc_url=None, lifespan=_lifespan)


def _error(status: int, kind: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"type": kind, "message": message}}, status_code=status, headers=headers)


async def _healthy(base: str) -> bool:
    try:
        response = await client.get(f"{base}/health", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@app.get("/healthz")
@app.get("/readyz")
async def healthz() -> JSONResponse:
    if await _healthy(UPSTREAMS[DEFAULT_MODEL]):
        return JSONResponse({"status": "ready", "model": DEFAULT_MODEL, "engine": "vllm"})
    return JSONResponse({"status": "not_ready", "model": DEFAULT_MODEL}, status_code=503)


@app.get("/v1/models")
async def models() -> JSONResponse:
    data = []
    for name, base in UPSTREAMS.items():
        if await _healthy(base):
            data.append({"id": name, "object": "model", "owned_by": "taide", "engine": "vllm"})
    return JSONResponse({"object": "list", "data": data})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error(400, "invalid_request", "Body must be JSON")

    model = str(body.get("model") or DEFAULT_MODEL)
    base = UPSTREAMS.get(model)
    if base is None:
        # Legacy callers sometimes send other names (e.g. "openai/taide-12b"); route to prod.
        model, base = DEFAULT_MODEL, UPSTREAMS[DEFAULT_MODEL]
    body["model"] = model

    requested = body.get("max_tokens") or body.get("max_completion_tokens") or MAX_OUTPUT_TOKENS
    try:
        body["max_tokens"] = max(1, min(int(requested), MAX_OUTPUT_TOKENS))
    except (TypeError, ValueError):
        body["max_tokens"] = MAX_OUTPUT_TOKENS
    body.pop("max_completion_tokens", None)
    if body.get("temperature") is None:
        body["temperature"] = DEFAULT_TEMPERATURE

    started = time.perf_counter()
    url = f"{base}/v1/chat/completions"
    if body.get("stream"):
        upstream_request = client.build_request("POST", url, json=body)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as error:
            return _error(503, "upstream_unavailable", f"vLLM unavailable: {type(error).__name__}", {"Retry-After": "2"})

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "text/event-stream"))

    try:
        upstream = await client.post(url, json=body)
    except httpx.HTTPError as error:
        return _error(503, "upstream_unavailable", f"vLLM unavailable: {type(error).__name__}", {"Retry-After": "2"})
    if upstream.status_code != 200:
        return Response(upstream.content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))
    result = upstream.json()
    result.setdefault("performance", {})["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return JSONResponse(result)


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    try:
        upstream = await client.post(f"{EMBEDDING_URL}/v1/embeddings", content=await request.body(),
                                     headers={"Content-Type": "application/json"}, timeout=30.0)
    except httpx.HTTPError as error:
        return _error(503, "upstream_unavailable", f"embedding server unavailable: {type(error).__name__}")
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("TAIDE_BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
