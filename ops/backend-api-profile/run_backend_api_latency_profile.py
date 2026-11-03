#!/usr/bin/env python3
"""
Direct BLINK :8010 backend API latency profiler.

Scope:
    client -> :8010 -> Agent/dependencies/model -> :8010 response

This intentionally excludes Vercel / Cloudflare / browser rendering.

All generated test artifacts are written below:
    /var/lib/jenkins/tomod/ops/backend-api-profile/runs/<timestamp>/
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import datetime as dt
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(os.environ.get("ROOT", "/var/lib/jenkins/tomod"))
DEFAULT_OPS = ROOT / "ops" / "backend-api-profile"
RUNTIME_LOG_DIR = ROOT / "ops" / "runtime" / "logs"

SERVICE_LOGS = {
    "backend-8010": RUNTIME_LOG_DIR / "backend-8010.log",
    "taide-8000": RUNTIME_LOG_DIR / "taide-8000.log",
    "router-8001": RUNTIME_LOG_DIR / "router-8001.log",
    "embedding-8002": RUNTIME_LOG_DIR / "embedding-8002.log",
    "litellm-8003": RUNTIME_LOG_DIR / "litellm-8003.log",
    "auth-8011": RUNTIME_LOG_DIR / "auth-8011.log",
}

SCENARIOS = {
    "product_search": {
        "flow": "product_search",
        "message": "請推薦適合混合肌、無香料、1000 元內的清爽保濕產品。",
    },
    "skin_consult": {
        "flow": "skin_consult",
        "message": "玻尿酸與神經醯胺在日常保濕上有什麼差異？請用繁體中文簡短回答。",
    },
    "auto_root": {
        "flow": None,
        "message": "我想了解玻尿酸和神經醯胺的差別。",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def json_dump(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, value: dict, lock: threading.Lock) -> None:
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def listener_pid(port: int) -> str | None:
    """Best-effort listener PID lookup.

    Runtime snapshotting is diagnostic only and must never delay or abort the
    benchmark. Query only the requested port instead of enumerating every
    socket/process on the host.
    """
    ss_bin = shutil.which("ss")
    if not ss_bin:
        return None

    try:
        result = subprocess.run(
            [ss_bin, "-H", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        marker = "pid="
        if marker not in line:
            continue
        tail = line.split(marker, 1)[1]
        digits = []
        for ch in tail:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            return "".join(digits)

    return None


def safe_runtime_snapshot(path: Path) -> None:
    out = []
    out.append(f"timestamp_utc={utc_now()}")
    for port in (8000, 8001, 8002, 8003, 8010, 8011, 8200):
        pid = listener_pid(port)
        out.append(f"\n===== PORT {port} =====")
        out.append(f"pid={pid or 'NONE'}")
        if not pid:
            continue
        proc = Path("/proc") / pid
        for label, rel in (("exe", "exe"), ("cwd", "cwd")):
            try:
                out.append(f"{label}={os.path.realpath(proc / rel)}")
            except Exception:
                pass
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            ).strip()
            out.append(f"cmd={cmd}")
        except Exception:
            pass

        if port == 8010:
            try:
                env_items = (proc / "environ").read_bytes().split(b"\0")
                env = {}
                for item in env_items:
                    if b"=" in item:
                        k, v = item.split(b"=", 1)
                        env[k.decode(errors="replace")] = v.decode(errors="replace")

                safe_keys = (
                    "TAIDE_BASE_URL",
                    "OPENAI_BASE_URL",
                    "OPENAI_INTENT_MODEL",
                    "KNOWLEDGE_RESPONSE_PROVIDER",
                    "KNOWLEDGE_RESPONSE_MODEL",
                    "PRODUCT_INGREDIENT_EXTRACTOR",
                    "MODEL_PROVIDER",
                    "MODEL_NAME",
                    "CHAT_MAX_RUNNING",
                    "CHAT_MAX_QUEUED",
                    "CHAT_QUEUE_TIMEOUT_SECONDS",
                    "CHAT_MAX_INPUT_TOKENS",
                    "CHAT_MAX_OUTPUT_TOKENS",
                    "EMBEDDING_MAX_CONCURRENT",
                    "EMBEDDING_QUEUE_TIMEOUT_SECONDS",
                )
                out.append("--- selected runtime env ---")
                for key in safe_keys:
                    out.append(f"{key}={env.get(key, '<MISSING_USE_CODE_DEFAULT>')}")
            except Exception as exc:
                out.append(f"runtime_env_error={type(exc).__name__}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def snapshot_log_sizes() -> dict[str, int]:
    sizes = {}
    for name, path in SERVICE_LOGS.items():
        try:
            sizes[name] = path.stat().st_size
        except FileNotFoundError:
            sizes[name] = 0
    return sizes


def copy_log_delta(src: Path, start: int, dst: Path) -> None:
    if not src.exists():
        dst.write_text("[source log missing]\n", encoding="utf-8")
        return
    size = src.stat().st_size
    if size < start:
        start = 0
    with src.open("rb") as f:
        f.seek(start)
        data = f.read()
    dst.write_bytes(data)


def start_monitors(run_dir: Path) -> list[subprocess.Popen]:
    procs: list[subprocess.Popen] = []

    vmstat = shutil.which("vmstat")
    if vmstat:
        f = (run_dir / "system-vmstat.log").open("wb")
        procs.append(
            subprocess.Popen(
                [vmstat, "1"],
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )

    amd_smi = shutil.which("amd-smi")
    if amd_smi:
        command = (
            "while true; do "
            "date -u '+%Y-%m-%dT%H:%M:%S.%3NZ'; "
            f"{amd_smi} monitor -g 0 2>&1; "
            "sleep 1; "
            "done"
        )
        f = (run_dir / "amd-smi.log").open("wb")
        procs.append(
            subprocess.Popen(
                ["bash", "-lc", command],
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )

    ss_bin = shutil.which("ss")
    if ss_bin:
        # Process-name resolution (-p) is intentionally avoided here because
        # it can stall on large/shared hosts. We only need timestamped socket
        # activity for correlation.
        command = (
            "while true; do "
            "date -u '+%Y-%m-%dT%H:%M:%S.%3NZ'; "
            f"timeout 1s {ss_bin} -Htn state established 2>/dev/null "
            "| grep -E ':8000|:8001|:8002|:8003|:8010|:8200' || true; "
            "sleep 0.5; "
            "done"
        )
        f = (run_dir / "connections.log").open("wb")
        procs.append(
            subprocess.Popen(
                ["bash", "-lc", command],
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )

    return procs


def stop_monitors(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.time() + 5
    for proc in procs:
        remaining = max(0.1, deadline - time.time())
        with contextlib.suppress(Exception):
            proc.wait(timeout=remaining)
    for proc in procs:
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(proc.pid, signal.SIGKILL)


class Client:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.guest_user_id: str | None = None

    def issue_guest(self) -> dict:
        req = urllib.request.Request(
            self.base_url + "/api/auth/guest-session",
            data=b"",
            method="POST",
        )
        started = time.perf_counter()
        with self.opener.open(req, timeout=self.timeout) as resp:
            body = resp.read()
            status = int(resp.status)
        elapsed_ms = (time.perf_counter() - started) * 1000
        payload = json.loads(body.decode("utf-8"))
        self.guest_user_id = str(payload.get("user_id", ""))
        return {
            "status": status,
            "elapsed_ms": round(elapsed_ms, 2),
            "user_id": self.guest_user_id,
        }

    def chat(
        self,
        *,
        scenario: str,
        flow: str | None,
        message: str,
        conversation_id: str,
        barrier: threading.Barrier | None = None,
        vu_index: int = 1,
        vu_level: int = 1,
        phase: str,
    ) -> dict:
        request_id = f"profile-{uuid.uuid4().hex}"
        payload = {
            "conversation_id": conversation_id,
            "message": message,
        }
        if flow is not None:
            payload["flow"] = flow

        if barrier is not None:
            barrier.wait()

        started_wall = utc_now()
        started = time.perf_counter()
        headers_at = None
        body_at = None
        status = None
        response_request_id = None
        body = b""
        error = None

        req = urllib.request.Request(
            self.base_url + "/api/chat/message",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": request_id,
                "X-Request-ID": request_id,
                "User-Agent": "BLINK-Backend-Latency-Profiler/1",
            },
            method="POST",
        )

        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                headers_at = time.perf_counter()
                status = int(resp.status)
                response_request_id = resp.headers.get("X-Request-ID")
                body = resp.read()
                body_at = time.perf_counter()
        except urllib.error.HTTPError as exc:
            headers_at = time.perf_counter()
            status = int(exc.code)
            response_request_id = exc.headers.get("X-Request-ID")
            body = exc.read()
            body_at = time.perf_counter()
            error = f"HTTPError:{exc.code}"
        except Exception as exc:
            body_at = time.perf_counter()
            error = f"{type(exc).__name__}:{exc}"

        if headers_at is None:
            headers_at = body_at
        if body_at is None:
            body_at = time.perf_counter()

        parsed = {}
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = {}

        reply = parsed.get("reply") if isinstance(parsed, dict) else None
        route = parsed.get("route") if isinstance(parsed, dict) else None
        stage = parsed.get("stage") if isinstance(parsed, dict) else None

        return {
            "phase": phase,
            "scenario": scenario,
            "vu": vu_level,
            "vu_index": vu_index,
            "guest_user_hash": (
                hashlib.sha256((self.guest_user_id or "").encode()).hexdigest()[:12]
                if self.guest_user_id
                else None
            ),
            "request_id": request_id,
            "response_request_id": response_request_id,
            "request_id_match": response_request_id == request_id,
            "conversation_id": conversation_id,
            "flow": flow,
            "started_at": started_wall,
            "status": status,
            "headers_ms": round((headers_at - started) * 1000, 2),
            "body_complete_ms": round((body_at - started) * 1000, 2),
            "reply_nonempty": isinstance(reply, str) and bool(reply.strip()),
            "reply_chars": len(reply) if isinstance(reply, str) else 0,
            "route": route,
            "stage": stage,
            "error": error,
        }


def health_probe(base_url: str, timeout: float) -> dict:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/v1/health")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "status": int(resp.status),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "body": body[:2000],
            }
    except Exception as exc:
        return {
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}:{exc}",
        }


def run_group(
    clients: list[Client],
    *,
    scenario_name: str,
    vu: int,
    phase: str,
    results_path: Path,
    results_lock: threading.Lock,
) -> list[dict]:
    cfg = SCENARIOS[scenario_name]
    barrier = threading.Barrier(vu + 1)
    group_id = uuid.uuid4().hex[:12]

    def worker(index: int) -> dict:
        conversation_id = (
            f"backend-profile-{phase}-{scenario_name}-vu{vu}-"
            f"{group_id}-{index:02d}"
        )
        row = clients[index - 1].chat(
            scenario=scenario_name,
            flow=cfg["flow"],
            message=cfg["message"],
            conversation_id=conversation_id,
            barrier=barrier,
            vu_index=index,
            vu_level=vu,
            phase=phase,
        )
        append_jsonl(results_path, row, results_lock)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=vu) as pool:
        futures = [pool.submit(worker, i) for i in range(1, vu + 1)]
        barrier.wait()
        return [future.result() for future in futures]


def summarize(rows: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str, int], list[dict]] = {}
    for row in rows:
        key = (str(row["phase"]), str(row["scenario"]), int(row["vu"]))
        buckets.setdefault(key, []).append(row)

    summary = []
    for (phase, scenario, vu), items in sorted(buckets.items()):
        body = [float(x["body_complete_ms"]) for x in items]
        headers = [float(x["headers_ms"]) for x in items]
        success = [
            x for x in items
            if x.get("status") == 200
            and x.get("reply_nonempty")
        ]
        summary.append({
            "phase": phase,
            "scenario": scenario,
            "vu": vu,
            "requests": len(items),
            "http_200_nonempty": len(success),
            "success_rate": round(len(success) / len(items), 6),
            "request_id_match_rate": round(
                sum(bool(x.get("request_id_match")) for x in items) / len(items),
                6,
            ),
            "headers_p50_ms": round(percentile(headers, 0.50) or 0, 2),
            "headers_p95_ms": round(percentile(headers, 0.95) or 0, 2),
            "body_p50_ms": round(percentile(body, 0.50) or 0, 2),
            "body_p95_ms": round(percentile(body, 0.95) or 0, 2),
            "body_max_ms": round(max(body), 2),
        })
    return summary


def extract_correlated_events(
    backend_log: Path,
    rows: list[dict],
    out_jsonl: Path,
    out_csv: Path,
    unmatched_path: Path,
) -> None:
    wanted = {str(x["request_id"]) for x in rows}
    matched = set()
    events = []

    if backend_log.exists():
        for raw in backend_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            rid = obj.get("request_id")
            if rid not in wanted:
                continue
            matched.add(rid)
            event = {
                "request_id": rid,
                "timestamp": obj.get("timestamp"),
                "event": obj.get("event"),
                "pipeline": obj.get("pipeline"),
                "agent": obj.get("agent"),
                "stage": obj.get("stage"),
                "dependency": obj.get("dependency"),
                "operation": obj.get("operation"),
                "duration_ms": obj.get("duration_ms"),
                "status": obj.get("status"),
                "http_status": obj.get("http_status"),
                "message": obj.get("message"),
            }
            events.append(event)

    with out_jsonl.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    cols = [
        "request_id", "timestamp", "event", "pipeline", "agent", "stage",
        "dependency", "operation", "duration_ms", "status", "http_status",
        "message",
    ]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(events)

    unmatched = sorted(wanted - matched)
    unmatched_path.write_text("\n".join(unmatched) + ("\n" if unmatched else ""), encoding="utf-8")


def write_summary_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("smoke", "formal", "all"),
        default="all",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--ops-dir", default=str(DEFAULT_OPS))
    parser.add_argument("--vu-list", default="1,2,4,8")
    parser.add_argument(
        "--formal-scenarios",
        default="product_search,skin_consult,auto_root",
    )
    parser.add_argument("--cooldown-seconds", type=int, default=65)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()

    vu_values = [int(x) for x in args.vu_list.split(",") if x.strip()]
    # Current guest-session bootstrap is rate-limited to 20 creations per
    # client IP / 600 s by the backend. Keep this profiler at VU<=16 so one
    # run remains below that production guard without changing service logic.
    if not vu_values or min(vu_values) < 1 or max(vu_values) > 16:
        raise SystemExit("vu-list must contain values from 1 through 16")
    formal_scenarios = [
        x.strip() for x in args.formal_scenarios.split(",") if x.strip()
    ]
    unknown = [x for x in formal_scenarios if x not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.ops_dir) / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)

    results_path = run_dir / "client-results.jsonl"
    results_lock = threading.Lock()
    rows: list[dict] = []

    json_dump(run_dir / "run-config.json", {
        "started_at": utc_now(),
        "mode": args.mode,
        "base_url": args.base_url,
        "vu_values": vu_values,
        "formal_scenarios": formal_scenarios,
        "cooldown_seconds": args.cooldown_seconds,
        "timeout_seconds": args.timeout_seconds,
        "scope": "direct_backend_8010_no_vercel_no_browser",
    })

    try:
        safe_runtime_snapshot(run_dir / "runtime-snapshot.txt")
    except Exception as exc:
        # Runtime snapshotting is observability-only. A host diagnostic failure
        # must not block the actual :8010 smoke/formal API test.
        (run_dir / "runtime-snapshot-error.txt").write_text(
            f"{type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )

    status_script = ROOT / "ops" / "runtime" / "status-production.sh"
    if status_script.exists():
        result = subprocess.run(
            ["bash", str(status_script)],
            capture_output=True,
            text=True,
            check=False,
        )
        (run_dir / "service-status-before.txt").write_text(
            result.stdout + result.stderr,
            encoding="utf-8",
        )

    health = health_probe(args.base_url, args.timeout_seconds)
    json_dump(run_dir / "health.json", health)
    if health.get("status") != 200:
        print(f"[ERROR] 8010 health failed: {health}", file=sys.stderr)
        print(f"Artifacts: {run_dir}")
        return 3

    max_vu = 1 if args.mode == "smoke" else max(vu_values)
    clients = [Client(args.base_url, args.timeout_seconds) for _ in range(max_vu)]

    guest_results = []
    for i, client in enumerate(clients, start=1):
        try:
            item = client.issue_guest()
            item["client"] = i
            guest_results.append(item)
        except Exception as exc:
            guest_results.append({
                "client": i,
                "status": None,
                "error": f"{type(exc).__name__}:{exc}",
            })
    json_dump(run_dir / "guest-sessions.json", guest_results)

    if any(x.get("status") != 200 for x in guest_results):
        print("[ERROR] guest session setup failed", file=sys.stderr)
        print(f"Artifacts: {run_dir}")
        return 3

    log_offsets = snapshot_log_sizes()
    json_dump(run_dir / "service-log-offsets.json", log_offsets)
    monitors = start_monitors(run_dir)

    test_failed = False

    try:
        if args.mode in ("smoke", "all"):
            smoke_cfg = SCENARIOS["skin_consult"]
            row = clients[0].chat(
                scenario="skin_consult",
                flow=smoke_cfg["flow"],
                message=smoke_cfg["message"],
                conversation_id=f"backend-profile-smoke-{uuid.uuid4().hex[:12]}",
                vu_index=1,
                vu_level=1,
                phase="smoke",
            )
            append_jsonl(results_path, row, results_lock)
            rows.append(row)

            smoke_ok = (
                row.get("status") == 200
                and row.get("reply_nonempty")
                and row.get("request_id_match")
            )
            json_dump(run_dir / "smoke-result.json", {
                "pass": smoke_ok,
                "row": row,
            })
            print(
                "[SMOKE]",
                "PASS" if smoke_ok else "FAIL",
                f"status={row.get('status')}",
                f"body_ms={row.get('body_complete_ms')}",
                f"request_id_match={row.get('request_id_match')}",
            )
            if not smoke_ok:
                test_failed = True

        if args.mode in ("formal", "all") and not test_failed:
            for scenario_index, scenario_name in enumerate(formal_scenarios):
                print(f"\n[FORMAL] scenario={scenario_name}")
                for vu in vu_values:
                    group = run_group(
                        clients,
                        scenario_name=scenario_name,
                        vu=vu,
                        phase="formal",
                        results_path=results_path,
                        results_lock=results_lock,
                    )
                    rows.extend(group)
                    ok = sum(
                        1 for x in group
                        if x.get("status") == 200 and x.get("reply_nonempty")
                    )
                    body_ms = [x["body_complete_ms"] for x in group]
                    print(
                        f"  VU={vu}",
                        f"ok={ok}/{len(group)}",
                        f"p50_ms={round(percentile(body_ms, 0.5) or 0, 2)}",
                        f"p95_ms={round(percentile(body_ms, 0.95) or 0, 2)}",
                    )
                    if ok != len(group):
                        test_failed = True

                if (
                    scenario_index < len(formal_scenarios) - 1
                    and args.cooldown_seconds > 0
                ):
                    print(
                        f"  cooldown={args.cooldown_seconds}s "
                        "(respects current localhost chat/conversation rate limits)"
                    )
                    time.sleep(args.cooldown_seconds)
    finally:
        stop_monitors(monitors)

    # Consolidate only the service-log bytes generated after the test started.
    service_delta_dir = run_dir / "service-log-deltas"
    service_delta_dir.mkdir(exist_ok=True)
    for name, src in SERVICE_LOGS.items():
        copy_log_delta(
            src,
            log_offsets.get(name, 0),
            service_delta_dir / f"{name}.log",
        )

    summary = summarize(rows)
    json_dump(run_dir / "summary.json", summary)
    write_summary_csv(run_dir / "summary.csv", summary)

    backend_delta = service_delta_dir / "backend-8010.log"
    extract_correlated_events(
        backend_delta,
        rows,
        run_dir / "correlated-backend-events.jsonl",
        run_dir / "correlated-backend-events.csv",
        run_dir / "unmatched-request-ids.txt",
    )

    if status_script.exists():
        result = subprocess.run(
            ["bash", str(status_script)],
            capture_output=True,
            text=True,
            check=False,
        )
        (run_dir / "service-status-after.txt").write_text(
            result.stdout + result.stderr,
            encoding="utf-8",
        )

    all_request_ids_match = all(bool(x.get("request_id_match")) for x in rows)
    all_http_ok = all(
        x.get("status") == 200 and x.get("reply_nonempty")
        for x in rows
    )

    final = {
        "finished_at": utc_now(),
        "run_dir": str(run_dir),
        "requests": len(rows),
        "all_http_200_nonempty": all_http_ok,
        "all_request_ids_match": all_request_ids_match,
        "test_failed": test_failed,
    }
    json_dump(run_dir / "final-status.json", final)

    print("\n============================================================")
    print("BACKEND API PROFILE COMPLETE")
    print("============================================================")
    print(json.dumps(final, ensure_ascii=False, indent=2))
    print(f"Summary: {run_dir / 'summary.csv'}")
    print(f"Trace:   {run_dir / 'correlated-backend-events.csv'}")

    return 0 if (not test_failed and all_http_ok and all_request_ids_match) else 2


if __name__ == "__main__":
    raise SystemExit(main())
