#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HARD_MAX_VUS = 128
DEFAULT_VU_LIST = "1,2,4,8,16,32,64"

def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")

def percentile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

def stats(xs):
    vals = [float(x) for x in xs if x is not None]
    if not vals:
        return {"mean": None, "p50": None, "p95": None, "max": None, "min": None}
    return {
        "mean": round(statistics.mean(vals), 4),
        "p50": round(percentile(vals, .50), 4),
        "p95": round(percentile(vals, .95), 4),
        "max": round(max(vals), 4),
        "min": round(min(vals), 4),
    }

def post_json(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    started = iso_now()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, raw, time.perf_counter() - t0, started, iso_now()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw, time.perf_counter() - t0, started, iso_now()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.perf_counter() - t0, started, iso_now()

def strip_markdown_fence(text: str):
    s = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", s, flags=re.S | re.I)
    return m.group(1).strip() if m else s


def _first_balanced_json_fragment(text: str):
    """Return the first balanced JSON object/array embedded in surrounding prose.

    This is used only for semantic validation. Strict validation still requires the
    whole response to be exactly valid JSON in the requested surface form.
    """
    for start, ch in enumerate(text):
        if ch not in "[{":
            continue

        stack = [ch]
        in_string = False
        escape = False

        for i in range(start + 1, len(text)):
            c = text[i]

            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue

            if c == '"':
                in_string = True
                continue

            if c in "[{":
                stack.append(c)
            elif c in "]}":
                if not stack:
                    break
                opener = stack[-1]
                if (opener == "[" and c != "]") or (opener == "{" and c != "}"):
                    break
                stack.pop()
                if not stack:
                    return text[start:i + 1]

    return None


def parse_json_semantic(text: str):
    """Parse JSON for semantic validation.

    Accepted semantically:
    - raw JSON;
    - a full Markdown JSON fence;
    - one balanced JSON object/array embedded in explanatory prose.

    This relaxation does NOT affect strict validation.
    """
    raw = strip_markdown_fence(text)

    try:
        return json.loads(raw), None
    except Exception as first_error:
        fragment = _first_balanced_json_fragment(raw)
        if fragment is None:
            return None, str(first_error)
        try:
            return json.loads(fragment), None
        except Exception as fragment_error:
            return None, str(fragment_error)


def _semantic_contains(obj, values):
    """Recursively find any requested semantic value inside JSON content."""
    normalized = {
        str(v).strip().casefold()
        for v in values
        if v is not None
    }

    if isinstance(obj, str):
        return obj.strip().casefold() in normalized
    if isinstance(obj, list):
        return any(_semantic_contains(item, values) for item in obj)
    if isinstance(obj, dict):
        return any(_semantic_contains(value, values) for value in obj.values())
    return False


def validate(expect: dict | None, content: str, nonce: str):
    """Return strict_ok, semantic_ok, strict_error, semantic_error.

    strict = exact requested syntax/schema.
    semantic = underlying requested content, tolerating presentation wrappers.
    """
    if not expect:
        return True, True, None, None

    typ = expect.get("type")
    s = content.strip()

    if typ == "nonempty":
        ok = bool(s)
        err = None if ok else "empty"
        return ok, ok, err, err

    if typ == "contains":
        v = str(expect["value"])
        ok = v in content
        err = None if ok else f"missing {v!r}"
        return ok, ok, err, err

    if typ == "contains_any":
        vals = [str(x) for x in expect.get("values", [])]
        ok = any(v in content for v in vals)
        err = None if ok else f"none of {vals!r} found"
        return ok, ok, err, err

    if typ == "not_contains":
        v = str(expect["value"])
        ok = v not in content
        err = None if ok else f"contains forbidden {v!r}"
        return ok, ok, err, err

    if typ == "exact_nonce":
        strict_ok = s == nonce
        semantic_ok = nonce in content
        return (
            strict_ok,
            semantic_ok,
            None if strict_ok else f"expected exact nonce {nonce!r}, got {content!r}",
            None if semantic_ok else f"nonce {nonce!r} not found in output",
        )

    if typ == "json_key":
        key = expect["value"]
        try:
            strict_obj = json.loads(s)
            strict_ok = isinstance(strict_obj, dict) and key in strict_obj
        except Exception:
            strict_ok = False
        obj, parse_err = parse_json_semantic(content)
        semantic_ok = isinstance(obj, dict) and key in obj
        return (
            strict_ok,
            semantic_ok,
            None if strict_ok else f"strict JSON object with key {key!r} required",
            None if semantic_ok else f"semantic JSON key {key!r} missing; parse={parse_err}",
        )

    if typ == "json_contains":
        key, val = expect["key"], expect["value"]
        semantic_values = [val] + [x for x in expect.get("semantic_values", []) if x != val]
        try:
            strict_obj = json.loads(s)
            arr = strict_obj.get(key) if isinstance(strict_obj, dict) else None
            strict_ok = isinstance(arr, list) and val in arr
        except Exception:
            strict_ok = False
        obj, parse_err = parse_json_semantic(content)
        arr = obj.get(key) if isinstance(obj, dict) else None
        semantic_ok = isinstance(arr, list) and _semantic_contains(arr, semantic_values)
        return (
            strict_ok,
            semantic_ok,
            None if strict_ok else f"strict JSON requires {val!r} in {key!r}",
            None if semantic_ok else f"none of {semantic_values!r} found in semantic {key!r}; parse={parse_err}",
        )

    if typ == "json_array":
        try:
            strict_obj = json.loads(s)
            strict_ok = isinstance(strict_obj, list)
        except Exception:
            strict_ok = False
        obj, parse_err = parse_json_semantic(content)
        semantic_ok = isinstance(obj, list)
        if not semantic_ok and isinstance(obj, dict) and len(obj) == 1:
            semantic_ok = isinstance(next(iter(obj.values())), list)
        return (
            strict_ok,
            semantic_ok,
            None if strict_ok else "strict output must be a raw JSON array without Markdown fence/object wrapper",
            None if semantic_ok else f"no list-like semantic payload; parse={parse_err}",
        )

    return False, False, f"unknown validation type {typ!r}", f"unknown validation type {typ!r}"

def render(s: str, nonce: str):
    return s.replace("{{NONCE}}", nonce)

def read_cpu_times():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()
    nums = [int(x) for x in fields[1:]]
    total = sum(nums)
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return total, idle

def cpu_util(prev, cur):
    dt = cur[0] - prev[0]
    di = cur[1] - prev[1]
    if dt <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (dt - di) / dt))

def read_meminfo():
    vals = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, rest = line.split(":", 1)
        vals[key] = int(rest.strip().split()[0])  # kB
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", 0)
    used = max(0, total - avail)
    return {
        "ram_used_gb": used / 1024 / 1024,
        "ram_total_gb": total / 1024 / 1024,
        "ram_used_pct": (used / total * 100.0) if total else None,
    }

_GPU_PREFIX = (
    r"^\s*(?P<gpu>\d+)\s+"
    r"(?:(?P<xcp>\d+)\s+)?"
    r"(?P<power>\d+(?:\.\d+)?)\s*W\s+"
    r"(?P<gpu_t>\d+(?:\.\d+)?)\s*°?\s*C\s+"
    r"(?P<mem_t>\d+(?:\.\d+)?)\s*°?\s*C\s+"
    r"(?P<gfx_clk>\d+(?:\.\d+)?)\s*MHz\s+"
    r"(?P<gfx_pct>\d+(?:\.\d+)?)\s*%\s+"
    r"(?P<mem_pct>\d+(?:\.\d+)?)\s*%\s+"
    r"(?:N/A|\d+(?:\.\d+)?\s*%)\s+"
    r"(?:N/A|\d+(?:\.\d+)?\s*%)\s+"
)
_GPU_SLASH = re.compile(
    _GPU_PREFIX +
    r"(?P<used>\d+(?:\.\d+)?)\s*/\s*(?P<total>\d+(?:\.\d+)?)\s*(?P<unit>GB|MB)\s*$"
)
_GPU_SPLIT = re.compile(
    _GPU_PREFIX +
    r"(?P<used>\d+(?:\.\d+)?)\s*(?P<used_unit>GB|MB)\s+"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>GB|MB)\s*$"
)

def _to_gb(v, unit):
    return float(v) if unit == "GB" else float(v) / 1024.0

def parse_amd_smi_monitor(text: str, gpu_index: int):
    for line in text.splitlines():
        m = _GPU_SLASH.match(line)
        if m and int(m.group("gpu")) == gpu_index:
            d = m.groupdict()
            return {
                "gpu_index": gpu_index,
                "xcp": int(d["xcp"]) if d.get("xcp") is not None else None,
                "power_w": float(d["power"]),
                "gpu_temp_c": float(d["gpu_t"]),
                "mem_temp_c": float(d["mem_t"]),
                "gfx_clock_mhz": float(d["gfx_clk"]),
                "gfx_util_pct": float(d["gfx_pct"]),
                "mem_util_pct": float(d["mem_pct"]),
                "vram_used_gb": _to_gb(d["used"], d["unit"]),
                "vram_total_gb": _to_gb(d["total"], d["unit"]),
            }
        m = _GPU_SPLIT.match(line)
        if m and int(m.group("gpu")) == gpu_index:
            d = m.groupdict()
            return {
                "gpu_index": gpu_index,
                "xcp": int(d["xcp"]) if d.get("xcp") is not None else None,
                "power_w": float(d["power"]),
                "gpu_temp_c": float(d["gpu_t"]),
                "mem_temp_c": float(d["mem_t"]),
                "gfx_clock_mhz": float(d["gfx_clk"]),
                "gfx_util_pct": float(d["gfx_pct"]),
                "mem_util_pct": float(d["mem_pct"]),
                "vram_used_gb": _to_gb(d["used"], d["used_unit"]),
                "vram_total_gb": _to_gb(d["total"], d["total_unit"]),
            }
    return None

class TelemetrySampler:
    def __init__(self, outdir: Path, gpu_index: int, interval: float, required: bool):
        self.outdir = outdir
        self.gpu_index = gpu_index
        self.interval = interval
        self.required = required
        self.stop_event = threading.Event()
        self.thread = None
        self.samples = []
        self.errors = []
        self.prev_cpu = None
        self.started = None
        self.ended = None
        self.amd_smi = shutil.which("amd-smi")

    def preflight(self):
        if not Path("/proc/stat").is_file():
            raise RuntimeError("/proc/stat unavailable; CPU telemetry cannot run")
        if not self.amd_smi:
            if self.required:
                raise RuntimeError("amd-smi not found; GPU/VRAM telemetry is required")
            self.errors.append("amd-smi not found")
            return
        # One-shot sample. The harness itself controls the sampling interval.
        # Do not pass --iterations/-i here: AMD SMI requires it to be paired
        # with --watch/-w, and a nested watch loop would distort sample timing.
        cp = subprocess.run(
            [self.amd_smi, "monitor", "-g", str(self.gpu_index)],
            capture_output=True, text=True, timeout=20
        )
        if cp.returncode != 0:
            msg = f"amd-smi preflight rc={cp.returncode}: {cp.stderr.strip()[:1000]}"
            if self.required:
                raise RuntimeError(msg)
            self.errors.append(msg)
            return
        parsed = parse_amd_smi_monitor(cp.stdout, self.gpu_index)
        if not parsed:
            msg = "could not parse amd-smi monitor output"
            if self.required:
                raise RuntimeError(msg + "\n" + cp.stdout[:2000])
            self.errors.append(msg)

    def start(self):
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.prev_cpu = read_cpu_times()
        self.started = iso_now()
        self.thread = threading.Thread(target=self._loop, name="telemetry-sampler", daemon=True)
        self.thread.start()

    def _one_sample(self):
        ts = iso_now()
        cur_cpu = read_cpu_times()
        cpu_pct = cpu_util(self.prev_cpu, cur_cpu) if self.prev_cpu else None
        self.prev_cpu = cur_cpu
        mem = read_meminfo()
        sample = {
            "timestamp": ts,
            "cpu_util_pct": cpu_pct,
            **mem,
            "gpu_index": self.gpu_index,
            "gpu_xcp": None,
            "gpu_power_w": None,
            "gpu_temp_c": None,
            "gpu_mem_temp_c": None,
            "gpu_gfx_clock_mhz": None,
            "gpu_gfx_util_pct": None,
            "gpu_mem_util_pct": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
        }
        if self.amd_smi:
            try:
                # One-shot AMD SMI snapshot; outer telemetry loop controls cadence.
                cp = subprocess.run(
                    [self.amd_smi, "monitor", "-g", str(self.gpu_index)],
                    capture_output=True, text=True, timeout=20
                )
                if cp.returncode != 0:
                    raise RuntimeError(f"amd-smi rc={cp.returncode}: {cp.stderr.strip()[:500]}")
                g = parse_amd_smi_monitor(cp.stdout, self.gpu_index)
                if not g:
                    raise RuntimeError("unparseable amd-smi monitor output")
                sample.update({
                    "gpu_xcp": g["xcp"],
                    "gpu_power_w": g["power_w"],
                    "gpu_temp_c": g["gpu_temp_c"],
                    "gpu_mem_temp_c": g["mem_temp_c"],
                    "gpu_gfx_clock_mhz": g["gfx_clock_mhz"],
                    "gpu_gfx_util_pct": g["gfx_util_pct"],
                    "gpu_mem_util_pct": g["mem_util_pct"],
                    "vram_used_gb": g["vram_used_gb"],
                    "vram_total_gb": g["vram_total_gb"],
                })
            except Exception as e:
                self.errors.append(f"{ts}: {type(e).__name__}: {e}")
        self.samples.append(sample)
        return sample

    def _loop(self):
        next_at = time.monotonic() + self.interval
        while not self.stop_event.wait(max(0, next_at - time.monotonic())):
            self._one_sample()
            next_at += self.interval

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=max(5.0, self.interval + 2))
        # Always take one final sample so short tests still record end-state VRAM.
        try:
            self._one_sample()
        except Exception as e:
            self.errors.append(f"final sample: {type(e).__name__}: {e}")
        self.ended = iso_now()
        self._write_files()

    def _write_files(self):
        jsonl = self.outdir / "telemetry.jsonl"
        with jsonl.open("w", encoding="utf-8") as f:
            for s in self.samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        fields = [
            "timestamp", "cpu_util_pct", "ram_used_gb", "ram_total_gb", "ram_used_pct",
            "gpu_index", "gpu_xcp", "gpu_power_w", "gpu_temp_c", "gpu_mem_temp_c",
            "gpu_gfx_clock_mhz", "gpu_gfx_util_pct", "gpu_mem_util_pct",
            "vram_used_gb", "vram_total_gb"
        ]
        with (self.outdir / "telemetry.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for s in self.samples:
                w.writerow({k: s.get(k) for k in fields})

    def summary(self):
        s = self.samples
        def vals(key): return [x.get(key) for x in s if x.get(key) is not None]
        vram = vals("vram_used_gb")
        out = {
            "enabled": True,
            "required": self.required,
            "gpu_index": self.gpu_index,
            "sample_interval_s": self.interval,
            "sample_count": len(s),
            "started": self.started,
            "ended": self.ended,
            "errors": self.errors,
            "cpu": {
                "logical_cpus": os.cpu_count(),
                "util_pct": stats(vals("cpu_util_pct")),
                "ram_used_gb": stats(vals("ram_used_gb")),
                "ram_used_pct": stats(vals("ram_used_pct")),
            },
            "gpu": {
                "gfx_util_pct": stats(vals("gpu_gfx_util_pct")),
                "mem_util_pct": stats(vals("gpu_mem_util_pct")),
                "power_w": stats(vals("gpu_power_w")),
                "gfx_clock_mhz": stats(vals("gpu_gfx_clock_mhz")),
                "gpu_temp_c": stats(vals("gpu_temp_c")),
                "mem_temp_c": stats(vals("gpu_mem_temp_c")),
                "vram_used_gb": {
                    **stats(vram),
                    "start": round(vram[0], 4) if vram else None,
                    "end": round(vram[-1], 4) if vram else None,
                    "delta_end_minus_start": round(vram[-1] - vram[0], 4) if len(vram) >= 2 else None,
                },
                "vram_total_gb": round(vals("vram_total_gb")[-1], 4) if vals("vram_total_gb") else None,
            },
        }
        return out

@dataclass
class VuState:
    vu: int
    nonce: str
    scenario: dict
    messages: list

def warmup(endpoint, model, count, timeout, max_tokens):
    for i in range(count):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "你的輸出必須完全等於兩個 ASCII 字元 OK。不要輸出中文、標點、空白或其他文字。"}],
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": min(max_tokens, 16),
        }
        status, body, lat, *_ = post_json(endpoint, payload, timeout)
        print(f"[WARMUP {i+1}/{count}] status={status} latency_s={lat:.4f}")
        if status != 200:
            raise RuntimeError(f"warmup failed: HTTP {status}: {body[:1000]}")

def select_scenarios(all_scenarios, scenario_name):
    if not scenario_name:
        return all_scenarios
    matches = [s for s in all_scenarios if s["name"] == scenario_name]
    if not matches:
        names = ", ".join(s["name"] for s in all_scenarios)
        raise SystemExit(f"unknown --scenario {scenario_name!r}; available: {names}")
    return matches


def check_turn_contract(scenarios, turns):
    for s in scenarios:
        n = len(s["turns"])
        if turns > n:
            raise SystemExit(
                f"--turns {turns} exceeds scenario {s['name']!r} length {n}. "
                "Implicit scenario repetition is disabled. "
                "Use --conversations-per-vu for independent repetitions."
            )


def run_once(args, vus: int, outdir: Path, run_label: str):
    all_scenarios = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    scenarios = select_scenarios(all_scenarios, args.scenario)
    check_turn_contract(scenarios, args.turns)
    endpoint = args.url.rstrip("/") + "/v1/chat/completions"
    outdir.mkdir(parents=True, exist_ok=True)
    event_log = outdir / "events.jsonl"

    telemetry = None
    if args.telemetry != "off":
        telemetry = TelemetrySampler(
            outdir=outdir,
            gpu_index=args.gpu,
            interval=args.telemetry_interval,
            required=(args.telemetry == "required"),
        )
        telemetry.preflight()
        telemetry.start()

    lock = threading.Lock()
    barrier = threading.Barrier(vus)
    rows = []
    run_epoch = int(time.time())

    states = []
    for vu in range(vus):
        sc = scenarios[vu % len(scenarios)]
        states.append(VuState(vu + 1, "", sc, []))

    def write_event(obj):
        with lock:
            with event_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def one_vu(st: VuState):
        local = []
        for conv_index in range(1, args.conversations_per_vu + 1):
            nonce = f"{run_label}-VU{st.vu:03d}-C{conv_index:03d}-{run_epoch}"
            messages = [{"role": "system", "content": render(st.scenario["system"], nonce)}]
            for turn_index in range(args.turns):
                spec = st.scenario["turns"][turn_index]
                user_text = render(spec["user"], nonce)
                messages.append({"role": "user", "content": user_text})
                try:
                    barrier.wait(timeout=args.barrier_timeout)
                except threading.BrokenBarrierError:
                    row = {
                        "timestamp_start": iso_now(), "timestamp_end": iso_now(),
                        "label": run_label, "vu": st.vu, "conversation": conv_index,
                        "scenario": st.scenario["name"], "turn": turn_index + 1,
                        "status": None, "latency_s": 0.0, "http_ok": False,
                        "strict_validation_ok": False, "semantic_validation_ok": False,
                        "strict_error": "barrier broken", "semantic_error": "barrier broken",
                        "prompt_tokens": None, "completion_tokens": None,
                        "total_tokens": None, "content": None,
                    }
                    write_event(row); local.append(row)
                    break

                payload = {
                    "model": args.model,
                    "messages": messages,
                    "temperature": args.temperature,
                    "top_p": 1.0,
                    "max_tokens": args.max_tokens,
                }
                status, body, latency, started, ended = post_json(endpoint, payload, args.timeout)
                row = {
                    "timestamp_start": started, "timestamp_end": ended,
                    "label": run_label, "vu": st.vu, "conversation": conv_index,
                    "scenario": st.scenario["name"], "turn": turn_index + 1,
                    "status": status, "latency_s": round(latency, 4),
                    "http_ok": status == 200,
                    "strict_validation_ok": False, "semantic_validation_ok": False,
                    "strict_error": None, "semantic_error": None,
                    "prompt_tokens": None, "completion_tokens": None,
                    "total_tokens": None, "content": None,
                }
                if status == 200:
                    try:
                        data = json.loads(body)
                        content = data["choices"][0]["message"]["content"]
                        usage = data.get("usage") or {}
                        strict_ok, semantic_ok, strict_err, semantic_err = validate(
                            spec.get("expect"), content, nonce
                        )
                        row.update(
                            strict_validation_ok=strict_ok,
                            semantic_validation_ok=semantic_ok,
                            strict_error=strict_err,
                            semantic_error=semantic_err,
                            content=content,
                            prompt_tokens=usage.get("prompt_tokens"),
                            completion_tokens=usage.get("completion_tokens"),
                            total_tokens=usage.get("total_tokens"),
                        )
                        messages.append({"role": "assistant", "content": content})
                    except Exception as e:
                        err = f"decode: {type(e).__name__}: {e}"
                        row["strict_error"] = err
                        row["semantic_error"] = err
                else:
                    row["strict_error"] = body[:2000]
                    row["semantic_error"] = body[:2000]
                write_event(row); local.append(row)
        return local

    print(
        f"[START] {run_label} url={args.url} vus={vus} turns={args.turns} "
        f"conversations_per_vu={args.conversations_per_vu} "
        f"scenario={args.scenario or 'balanced_round_robin'}"
    )
    print(f"[START] {iso_now()}")
    t0 = time.perf_counter()
    try:
        with cf.ThreadPoolExecutor(max_workers=vus) as ex:
            futs = [ex.submit(one_vu, st) for st in states]
            for fut in futs:
                rows.extend(fut.result())
    finally:
        wall = time.perf_counter() - t0
        if telemetry:
            telemetry.stop()
    print(f"[END] {iso_now()} wall_s={wall:.4f}")

    ok_rows = [r for r in rows if r.get("status") == 200]
    strict_rows = [r for r in ok_rows if r.get("strict_validation_ok")]
    semantic_rows = [r for r in ok_rows if r.get("semantic_validation_ok")]
    lats = [r["latency_s"] for r in ok_rows]
    completion_tokens = sum((r.get("completion_tokens") or 0) for r in ok_rows)
    total_tokens = sum((r.get("total_tokens") or 0) for r in ok_rows)

    def grouped(field):
        groups = {}
        for r in rows:
            groups.setdefault(str(r[field]), []).append(r)
        out = {}
        for key, rr in sorted(groups.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0]):
            good = [x for x in rr if x.get("status") == 200]
            ls = [x["latency_s"] for x in good]
            out[key] = {
                "requests": len(rr),
                "http_200": len(good),
                "strict_validation_pass": sum(1 for x in good if x.get("strict_validation_ok")),
                "semantic_validation_pass": sum(1 for x in good if x.get("semantic_validation_ok")),
                "p50_s": round(percentile(ls, .5), 4) if ls else None,
                "p95_s": round(percentile(ls, .95), 4) if ls else None,
                "max_prompt_tokens": max((x.get("prompt_tokens") or 0) for x in good) if good else None,
            }
        return out

    scenario_groups = {}
    for r in rows:
        scenario_groups.setdefault(r["scenario"], []).append(r)
    per_scenario = {}
    for name, rr in scenario_groups.items():
        good = [x for x in rr if x.get("status") == 200]
        per_scenario[name] = {
            "requests": len(rr),
            "http_200": len(good),
            "strict_validation_pass_rate": round(
                sum(1 for x in good if x.get("strict_validation_ok")) / len(good), 6
            ) if good else 0,
            "semantic_validation_pass_rate": round(
                sum(1 for x in good if x.get("semantic_validation_ok")) / len(good), 6
            ) if good else 0,
        }

    summary = {
        "schema_version": 3,
        "label": run_label,
        "url": args.url,
        "model": args.model,
        "scenario_mode": args.scenario or "balanced_round_robin",
        "vus": vus,
        "turns_per_conversation": args.turns,
        "conversations_per_vu": args.conversations_per_vu,
        "planned_conversations": vus * args.conversations_per_vu,
        "planned_requests": vus * args.conversations_per_vu * args.turns,
        "completed_rows": len(rows),
        "http_200": len(ok_rows),
        "http_success_rate": round(len(ok_rows) / len(rows), 6) if rows else 0,
        "strict_validation_pass": len(strict_rows),
        "strict_validation_pass_rate": round(len(strict_rows) / len(ok_rows), 6) if ok_rows else 0,
        "semantic_validation_pass": len(semantic_rows),
        "semantic_validation_pass_rate": round(len(semantic_rows) / len(ok_rows), 6) if ok_rows else 0,
        "validation_pass": len(strict_rows),
        "validation_pass_rate": round(len(strict_rows) / len(ok_rows), 6) if ok_rows else 0,
        "wall_s": round(wall, 4),
        "request_rps": round(len(ok_rows) / wall, 4) if wall else None,
        "completion_tokens_per_s": round(completion_tokens / wall, 4) if wall else None,
        "total_tokens_per_s": round(total_tokens / wall, 4) if wall else None,
        "latency_s": {
            "mean": round(statistics.mean(lats), 4) if lats else None,
            "p50": round(percentile(lats, .50), 4) if lats else None,
            "p95": round(percentile(lats, .95), 4) if lats else None,
            "p99": round(percentile(lats, .99), 4) if lats else None,
            "max": round(max(lats), 4) if lats else None,
        },
        "per_turn": grouped("turn"),
        "per_conversation": grouped("conversation"),
        "per_scenario": per_scenario,
        "telemetry": telemetry.summary() if telemetry else {"enabled": False},
    }

    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "rows.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[ARTIFACTS] {outdir.resolve()}")

    any_http_fail = len(ok_rows) != len(rows)
    any_semantic_fail = len(semantic_rows) != len(ok_rows)
    any_strict_fail = len(strict_rows) != len(ok_rows)
    return summary, (3 if any_http_fail else 2 if any_semantic_fail else 1 if any_strict_fail else 0)

def parse_vu_list(text, max_vus):
    values = []
    for raw in text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            v = int(raw)
        except ValueError:
            raise ValueError(f"invalid VU value: {raw!r}")
        if v < 1:
            raise ValueError(f"VU must be >=1, got {v}")
        if v > max_vus:
            raise ValueError(f"VU {v} exceeds configured --max-vus {max_vus}")
        if v not in values:
            values.append(v)
    if not values:
        raise ValueError("VU list is empty")
    return values

def write_sweep_csv(path: Path, summaries):
    fields = [
        "vus", "scenario_mode", "conversations_per_vu",
        "http_success_rate", "strict_validation_pass_rate", "semantic_validation_pass_rate",
        "wall_s", "request_rps", "completion_tokens_per_s",
        "latency_p50_s", "latency_p95_s", "latency_p99_s",
        "cpu_mean_pct", "cpu_p95_pct", "cpu_max_pct",
        "gpu_gfx_mean_pct", "gpu_gfx_p95_pct", "gpu_gfx_max_pct",
        "gpu_mem_mean_pct", "gpu_power_mean_w", "gpu_power_max_w",
        "vram_start_gb", "vram_end_gb", "vram_max_gb", "vram_delta_gb",
        "telemetry_samples",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in summaries:
            t = s.get("telemetry", {})
            cpu = t.get("cpu", {})
            gpu = t.get("gpu", {})
            vram = gpu.get("vram_used_gb", {})
            w.writerow({
                "vus": s["vus"],
                "scenario_mode": s["scenario_mode"],
                "conversations_per_vu": s["conversations_per_vu"],
                "http_success_rate": s["http_success_rate"],
                "strict_validation_pass_rate": s["strict_validation_pass_rate"],
                "semantic_validation_pass_rate": s["semantic_validation_pass_rate"],
                "wall_s": s["wall_s"],
                "request_rps": s["request_rps"],
                "completion_tokens_per_s": s["completion_tokens_per_s"],
                "latency_p50_s": s["latency_s"]["p50"],
                "latency_p95_s": s["latency_s"]["p95"],
                "latency_p99_s": s["latency_s"]["p99"],
                "cpu_mean_pct": cpu.get("util_pct", {}).get("mean"),
                "cpu_p95_pct": cpu.get("util_pct", {}).get("p95"),
                "cpu_max_pct": cpu.get("util_pct", {}).get("max"),
                "gpu_gfx_mean_pct": gpu.get("gfx_util_pct", {}).get("mean"),
                "gpu_gfx_p95_pct": gpu.get("gfx_util_pct", {}).get("p95"),
                "gpu_gfx_max_pct": gpu.get("gfx_util_pct", {}).get("max"),
                "gpu_mem_mean_pct": gpu.get("mem_util_pct", {}).get("mean"),
                "gpu_power_mean_w": gpu.get("power_w", {}).get("mean"),
                "gpu_power_max_w": gpu.get("power_w", {}).get("max"),
                "vram_start_gb": vram.get("start"),
                "vram_end_gb": vram.get("end"),
                "vram_max_gb": vram.get("max"),
                "vram_delta_gb": vram.get("delta_end_minus_start"),
                "telemetry_samples": t.get("sample_count"),
            })

def main(args):
    if args.max_vus < 1 or args.max_vus > HARD_MAX_VUS:
        raise SystemExit(f"--max-vus must be between 1 and {HARD_MAX_VUS}")
    if args.conversations_per_vu < 1:
        raise SystemExit("--conversations-per-vu must be >= 1")
    if args.vus is not None:
        if args.vus < 1 or args.vus > args.max_vus:
            raise SystemExit(f"--vus must be between 1 and {args.max_vus}")
        vu_values = [args.vus]
        sweep = False
    else:
        try:
            vu_values = parse_vu_list(args.vu_list, args.max_vus)
        except ValueError as e:
            raise SystemExit(str(e))
        sweep = True

    if args.telemetry_interval <= 0:
        raise SystemExit("--telemetry-interval must be > 0")

    root_out = Path(args.output_dir or f"multiturn-results-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    root_out.mkdir(parents=True, exist_ok=True)

    print(f"[PLAN] mode={'sweep' if sweep else 'single'} vus={vu_values} hard_max={HARD_MAX_VUS}")
    print(f"[PLAN] scenario={args.scenario or 'balanced_round_robin'} turns={args.turns} conversations_per_vu={args.conversations_per_vu}")
    print(f"[PLAN] scenarios_file={args.scenarios}")
    print(f"[PLAN] telemetry={args.telemetry} gpu={args.gpu} interval={args.telemetry_interval}s")
    print(f"[PLAN] output={root_out.resolve()}")

    summaries = []
    exit_code = 0
    for idx, vus in enumerate(vu_values):
        outdir = root_out / f"vu{vus}" if sweep else root_out
        label = f"{args.label}-VU{vus}"
        summary, rc = run_once(args, vus, outdir, label)
        summaries.append(summary)
        exit_code = max(exit_code, rc)
        if idx != len(vu_values) - 1 and args.pause_between > 0:
            print(f"[PAUSE] {args.pause_between}s before next VU")
            time.sleep(args.pause_between)

    if sweep:
        sweep_summary = {
            "schema_version": 3,
            "mode": "sweep",
            "url": args.url,
            "model": args.model,
            "scenario": args.scenario or "balanced_round_robin",
            "vu_values": vu_values,
            "turns_per_conversation": args.turns,
            "conversations_per_vu": args.conversations_per_vu,
            "scenarios_file": str(args.scenarios),
            "telemetry": args.telemetry,
            "gpu_index": args.gpu,
            "results": summaries,
        }
        (root_out / "sweep_summary.json").write_text(
            json.dumps(sweep_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_sweep_csv(root_out / "sweep_summary.csv", summaries)
        print(f"[SWEEP] summary={root_out/'sweep_summary.json'}")
        print(f"[SWEEP] csv={root_out/'sweep_summary.csv'}")

    return exit_code

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="BLINK vLLM multi-VU multi-turn benchmark with strict+semantic validation and CPU/GPU/VRAM telemetry."
    )
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", default="taide-12b")
    ap.add_argument("--scenarios", default=str(Path(__file__).with_name("multiturn_scenarios.json")))
    ap.add_argument("--scenario", default=None,
                    help="Fix every VU to one named scenario. Default: balanced round-robin across the scenario file.")
    ap.add_argument("--vus", type=int, default=None,
                    help="Run only this VU level. If omitted, run --vu-list sequentially.")
    ap.add_argument("--vu-list", default=DEFAULT_VU_LIST)
    ap.add_argument("--max-vus", type=int, default=HARD_MAX_VUS)
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--conversations-per-vu", type=int, default=1,
                    help="Independent fresh conversations per VU. Use this instead of repeating a scenario in one history.")
    ap.add_argument("--max-tokens", type=int, default=96)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--barrier-timeout", type=float, default=900.0)
    ap.add_argument("--pause-between", type=float, default=5.0)
    ap.add_argument("--label", default="VLLM8200-MT")
    ap.add_argument("--output-dir")
    ap.add_argument("--telemetry", choices=("required", "best-effort", "off"), default="required")
    ap.add_argument("--telemetry-interval", type=float, default=1.0)
    ap.add_argument("--gpu", type=int, default=0)
    raise SystemExit(main(ap.parse_args()))
