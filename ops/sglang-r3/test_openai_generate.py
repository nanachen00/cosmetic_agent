#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, time, urllib.error, urllib.request
from pathlib import Path

def get_json(url, timeout):
    req = urllib.request.Request(url, method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.perf_counter()-t0
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}, time.perf_counter()-t0

def post_json(url, payload, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace"), time.perf_counter()-t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), time.perf_counter()-t0
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.perf_counter()-t0

def validate(case, content):
    kind = case["validation"]["type"]
    value = case["validation"].get("value")
    if kind == "exact_normalized":
        ok = " ".join(content.split()).upper() == str(value).upper()
        return ok, None if ok else f"expected {value!r}"
    if kind == "nonempty":
        ok = bool(content.strip())
        return ok, None if ok else "empty content"
    if kind == "json_contains_ingredient":
        try:
            data = json.loads(content)
        except Exception as e:
            return False, f"invalid JSON: {e}"
        vals = data.get("ingredients")
        ok = isinstance(vals, list) and value in vals
        return ok, None if ok else f"ingredients={vals!r}"
    return False, f"unknown validation {kind}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="taide-12b")
    ap.add_argument("--cases", default=str(Path(__file__).with_name("taide_generate_cases.json")))
    ap.add_argument("--warmups", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--output")
    a = ap.parse_args()

    base = a.url.rstrip("/")
    ms, mb, ml = get_json(base + "/v1/models", 15)
    print(json.dumps({"models_status": ms, "models_latency_s": round(ml,4), "body": mb}, ensure_ascii=False, indent=2))
    if ms != 200:
        return 2

    payload = {
        "model": a.model,
        "messages": [{"role":"user","content":"只回答 OK"}],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 8,
    }

    warm = []
    for i in range(a.warmups):
        s,b,l = post_json(base + "/v1/chat/completions", payload, a.timeout)
        print(f"warmup {i+1}: status={s} latency_s={l:.4f}")
        warm.append(l)
        if s != 200:
            print(b[:2000])
            return 3

    cases = json.loads(Path(a.cases).read_text())
    rows = []
    bad = False
    for case in cases:
        p = {
            "model": a.model,
            "messages": case["messages"],
            "temperature": case.get("temperature", 0.0),
            "top_p": 1.0,
            "max_tokens": case.get("max_new_tokens", 64),
        }
        s,b,l = post_json(base + "/v1/chat/completions", p, a.timeout)
        row = {"name":case["name"],"status":s,"latency_s":round(l,4),"ok":False,"content":None,"usage":None,"error":None}
        if s == 200:
            try:
                data = json.loads(b)
                content = data["choices"][0]["message"]["content"]
                ok, err = validate(case, content)
                row.update(ok=ok, content=content, usage=data.get("usage"), error=err)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
        else:
            row["error"] = b[:2000]
        bad |= not row["ok"]
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=2))

    report = {
        "base_url": base,
        "model": a.model,
        "warmup_latencies_s": [round(x,4) for x in warm],
        "results": rows,
    }
    if a.output:
        Path(a.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("[OK] report=", a.output, sep="")
    return 1 if bad else 0

if __name__ == "__main__":
    raise SystemExit(main())
