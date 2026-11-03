#!/usr/bin/env python3
"""Closed-loop VU (virtual user) benchmark for an OpenAI-compatible chat endpoint.

Added 2026-10-22. Standard library only, so it runs in any Python 3.8+ env.
Each VU sends one streaming /v1/chat/completions request, waits for the full
reply, then immediately sends the next, for --duration seconds per VU level.

Per request: TTFT (time to first content token), end-to-end latency,
output tokens, TPOT ((latency - TTFT) / (tokens - 1)).
Per level: success/error counts, p50/p95/p99, request/s, output tokens/s,
GPU VRAM / utilisation samples (best effort via amd-smi / rocm-smi).

Example:
  python3 vu_bench.py --url http://127.0.0.1:8200 --model taide-12b \
      --vu-list 1,2,4,8,16,32,64 --duration 60 --output-dir results/vllm
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import random
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

PROMPTS_SHORT = [
    "我是乾性肌，冬天臉頰容易脫皮，早晚保養順序要怎麼安排？",
    "玻尿酸和神經醯胺的保濕作用有什麼不同？",
    "最近下巴一直冒痘痘，可能是什麼原因？日常保養要注意什麼？",
    "A酸可以和維他命C一起使用嗎？請說明使用時間與注意事項。",
    "敏感肌第一次使用酸類產品，應該怎麼建立耐受度？",
    "防曬乳的 SPF 和 PA 分別代表什麼？通勤族要怎麼選？",
    "混合肌 T 字部位出油、兩頰偏乾，洗臉產品要怎麼挑？",
    "菸鹼醯胺對淡化痘疤有幫助嗎？建議濃度大概多少？",
    "化妝水、精華液、乳液的差別是什麼？一定都要用嗎？",
    "孕期保養有哪些成分建議避免？",
    "黑眼圈分成哪幾種類型？各自可以怎麼改善？",
    "請用三點說明卸妝不乾淨可能造成的皮膚問題。",
]

# ~1.5k-token product/knowledge context, like the backend's RAG prompts.
LONG_CONTEXT = (
    "以下是商品與成分資料庫的檢索結果，請只根據資料回答，資料沒有的內容請說明不確定。\n"
    + "\n".join(
        f"[資料{i}] 商品：保濕修護精華 {i} 號；主要成分：玻尿酸、神經醯胺 NP、泛醇、積雪草萃取；"
        f"適用膚質：乾性、敏感性；使用方式：早晚潔膚後取適量均勻塗抹；注意事項：避免接觸眼睛，"
        f"若出現刺痛、泛紅請停用並諮詢醫師；容量 30ml；建議售價新台幣 {800 + i * 37} 元。"
        for i in range(1, 25)
    )
)


def percentile(values, p):
    """Linear-interpolated percentile (same definition as the team's harness)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(values):
    if not values:
        return {"mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 0.50), 4),
        "p95": round(percentile(values, 0.95), 4),
        "p99": round(percentile(values, 0.99), 4),
        "max": round(max(values), 4),
    }


def build_messages(input_profile, rng):
    question = rng.choice(PROMPTS_SHORT)
    if input_profile == "long":
        return [
            {"role": "system", "content": "你是 BLINK AI 美妝保養助理，使用繁體中文（台灣用語）回答。"},
            {"role": "user", "content": LONG_CONTEXT + "\n\n問題：" + question},
        ]
    return [
        {"role": "system", "content": "你是 BLINK AI 美妝保養助理，使用繁體中文（台灣用語）回答。"},
        {"role": "user", "content": question},
    ]


def one_request(url, model, messages, max_tokens, ignore_eos, timeout):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if ignore_eos:
        body["ignore_eos"] = True
    started = time.perf_counter()
    ttft = None
    chunks = 0
    usage_tokens = None
    text_parts = []
    try:
        conn.request("POST", "/v1/chat/completions", body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read(400).decode("utf-8", "replace")
            return {"ok": False, "error": f"HTTP {resp.status}: {detail}", "latency": time.perf_counter() - started}
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if event.get("usage") and event["usage"].get("completion_tokens") is not None:
                usage_tokens = int(event["usage"]["completion_tokens"])
            for choice in event.get("choices") or []:
                content = (choice.get("delta") or {}).get("content")
                if content:
                    if ttft is None:
                        ttft = time.perf_counter() - started
                    chunks += 1
                    text_parts.append(content)
        latency = time.perf_counter() - started
    except Exception as exc:  # timeouts, resets
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "latency": time.perf_counter() - started}
    finally:
        conn.close()
    tokens = usage_tokens if usage_tokens is not None else chunks
    if ttft is None or tokens == 0:
        return {"ok": False, "error": "empty completion", "latency": latency}
    tpot = (latency - ttft) / (tokens - 1) if tokens > 1 else None
    return {
        "ok": True,
        "ttft": ttft,
        "latency": latency,
        "tokens": tokens,
        "tokens_source": "usage" if usage_tokens is not None else "chunks",
        "tpot": tpot,
        "text_head": "".join(text_parts)[:60],
    }


class GpuSampler(threading.Thread):
    """Best-effort VRAM / utilisation sampling; never fails the benchmark."""

    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self.tool = "amd-smi" if shutil.which("amd-smi") else ("rocm-smi" if shutil.which("rocm-smi") else None)

    def _sample(self):
        if self.tool == "rocm-smi":
            out = subprocess.run(["rocm-smi", "--showuse", "--showmemuse", "--showmeminfo", "vram", "--json"],
                                 capture_output=True, text=True, timeout=10).stdout
            data = json.loads(out or "{}")
            card = next(iter(data.values()), {}) if data else {}
            used = next((v for k, v in card.items() if "VRAM Total Used" in k), None)
            total = next((v for k, v in card.items() if "VRAM Total Memory" in k), None)
            util = next((v for k, v in card.items() if "GPU use" in k), None)
            return {"vram_used_gb": float(used) / 2**30 if used else None,
                    "vram_total_gb": float(total) / 2**30 if total else None,
                    "gpu_util_pct": float(util) if util not in (None, "") else None}
        if self.tool == "amd-smi":
            out = subprocess.run(["amd-smi", "metric", "-u", "-m", "--json"],
                                 capture_output=True, text=True, timeout=10).stdout
            data = json.loads(out or "[]")
            gpu = data[0] if isinstance(data, list) and data else (data.get("gpu_data", [{}])[0] if isinstance(data, dict) else {})
            mem = gpu.get("mem_usage", {}) or {}
            usage = gpu.get("usage", {}) or {}

            def val(x):
                return x.get("value") if isinstance(x, dict) else x
            used, total = val(mem.get("used_vram")), val(mem.get("total_vram"))
            return {"vram_used_gb": float(used) / 1024 if used is not None else None,
                    "vram_total_gb": float(total) / 1024 if total is not None else None,
                    "gpu_util_pct": float(val(usage.get("gfx_activity"))) if val(usage.get("gfx_activity")) is not None else None}
        return None

    def run(self):
        while not self._stop.is_set() and self.tool:
            try:
                sample = self._sample()
                if sample:
                    self.samples.append(sample)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def summary(self):
        used = [s["vram_used_gb"] for s in self.samples if s.get("vram_used_gb") is not None]
        util = [s["gpu_util_pct"] for s in self.samples if s.get("gpu_util_pct") is not None]
        return {
            "tool": self.tool,
            "vram_max_gb": round(max(used), 2) if used else None,
            "gpu_util_mean_pct": round(statistics.fmean(util), 1) if util else None,
            "gpu_util_max_pct": round(max(util), 1) if util else None,
        }


def run_level(args, vus, out_dir):
    rows = []
    lock = threading.Lock()
    deadline = time.perf_counter() + args.duration
    sampler = GpuSampler()
    sampler.start()

    def worker(index):
        rng = random.Random(args.seed * 1000 + index)
        while time.perf_counter() < deadline:
            result = one_request(args.url, args.model, build_messages(args.input, rng),
                                 args.max_tokens, args.ignore_eos, args.timeout)
            result.update({"vu": index, "finished_at": time.time()})
            with lock:
                rows.append(result)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(vus)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    sampler.stop()

    ok = [r for r in rows if r["ok"]]
    errors = [r for r in rows if not r["ok"]]
    summary = {
        "vus": vus,
        "duration_s": round(wall, 2),
        "requests": len(rows),
        "success": len(ok),
        "errors": len(errors),
        "error_rate_pct": round(100 * len(errors) / len(rows), 2) if rows else None,
        "rps": round(len(ok) / wall, 3) if wall else None,
        "output_tokens_per_s": round(sum(r["tokens"] for r in ok) / wall, 1) if wall else None,
        "per_user_tokens_per_s": round(statistics.fmean([(r["tokens"] - 1) / (r["latency"] - r["ttft"]) for r in ok if r["tokens"] > 1 and r["latency"] > r["ttft"]]), 2) if ok else None,
        "ttft_s": stats([r["ttft"] for r in ok]),
        "tpot_ms": stats([r["tpot"] * 1000 for r in ok if r.get("tpot")]),
        "latency_s": stats([r["latency"] for r in ok]),
        "output_tokens": stats([r["tokens"] for r in ok]),
        "tokens_source": sorted({r["tokens_source"] for r in ok}),
        "sample_errors": sorted({r["error"][:160] for r in errors})[:5],
        "sample_reply": ok[0]["text_head"] if ok else None,
        "gpu": sampler.summary(),
    }
    level_dir = out_dir / f"vu{vus:03d}"
    level_dir.mkdir(parents=True, exist_ok=True)
    (level_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (level_dir / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return summary


def warmup(args):
    rng = random.Random(0)
    for _ in range(3):
        result = one_request(args.url, args.model, build_messages(args.input, rng), 32, False, args.timeout)
        if not result["ok"]:
            raise SystemExit(f"warm-up failed: {result['error']}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8200")
    parser.add_argument("--model", default="taide-12b")
    parser.add_argument("--label", default="")
    parser.add_argument("--vu-list", default="1,2,4,8,16,32,64")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds per VU level")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--ignore-eos", action="store_true", help="force exactly max-tokens outputs (fixed-length load)")
    parser.add_argument("--input", choices=["short", "long"], default="short")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[warmup] {args.url} model={args.model} input={args.input}", flush=True)
    sample = warmup(args)
    print(f"[warmup] ok, sample reply: {sample['text_head']}", flush=True)

    summaries = []
    for vus in [int(x) for x in args.vu_list.split(",") if x.strip()]:
        print(f"[run] VU={vus} for {args.duration:.0f}s ...", flush=True)
        s = run_level(args, vus, out_dir)
        summaries.append(s)
        print(f"      ok={s['success']} err={s['errors']} rps={s['rps']} tok/s={s['output_tokens_per_s']} "
              f"TTFT p50/p95={s['ttft_s']['p50']}/{s['ttft_s']['p95']}s TPOT p50={s['tpot_ms']['p50']}ms "
              f"VRAM max={s['gpu']['vram_max_gb']}GB", flush=True)
        if s["errors"]:
            print(f"      errors: {s['sample_errors']}", flush=True)

    meta = {"label": args.label, "url": args.url, "model": args.model, "input": args.input,
            "max_tokens": args.max_tokens, "ignore_eos": args.ignore_eos, "duration_s": args.duration,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (out_dir / "sweep.json").write_text(json.dumps({"meta": meta, "levels": summaries}, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    fields = ["label", "vus", "requests", "success", "errors", "error_rate_pct", "rps", "output_tokens_per_s",
              "per_user_tokens_per_s", "ttft_p50_s", "ttft_p95_s", "ttft_p99_s", "tpot_p50_ms", "tpot_p95_ms",
              "latency_p50_s", "latency_p95_s", "output_tokens_mean", "vram_max_gb", "gpu_util_mean_pct"]
    with (out_dir / "sweep.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for s in summaries:
            writer.writerow({
                "label": args.label, "vus": s["vus"], "requests": s["requests"], "success": s["success"],
                "errors": s["errors"], "error_rate_pct": s["error_rate_pct"], "rps": s["rps"],
                "output_tokens_per_s": s["output_tokens_per_s"], "per_user_tokens_per_s": s["per_user_tokens_per_s"],
                "ttft_p50_s": s["ttft_s"]["p50"], "ttft_p95_s": s["ttft_s"]["p95"], "ttft_p99_s": s["ttft_s"]["p99"],
                "tpot_p50_ms": s["tpot_ms"]["p50"], "tpot_p95_ms": s["tpot_ms"]["p95"],
                "latency_p50_s": s["latency_s"]["p50"], "latency_p95_s": s["latency_s"]["p95"],
                "output_tokens_mean": s["output_tokens"]["mean"], "vram_max_gb": s["gpu"]["vram_max_gb"],
                "gpu_util_mean_pct": s["gpu"]["gpu_util_mean_pct"],
            })
    print(f"[done] {out_dir}/sweep.csv", flush=True)
    worst = max((s["error_rate_pct"] or 0) for s in summaries) if summaries else 0
    raise SystemExit(0 if worst == 0 else 1)


if __name__ == "__main__":
    main()
