#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def resolve_execution(g: Path) -> Path | None:
    strict = read_json(g / "STRICT_ACCEPTANCE.json") or {}
    eid = strict.get("internal_execution_id")
    internal = g / "internal-full-path-shadow-ab"
    if isinstance(eid, str) and eid:
        p = internal / "executions" / Path(eid).name
        if p.is_dir():
            return p

    latest = internal / "LATEST_EXECUTION"
    if latest.is_file():
        try:
            p = internal / "executions" / Path(latest.read_text(encoding="utf-8").strip()).name
            if p.is_dir():
                return p
        except Exception:
            pass

    dirs = [p for p in (internal / "executions").glob("*") if p.is_dir()] if (internal / "executions").is_dir() else []
    complete = [p for p in dirs if (p / "comparison-summary.json").is_file()]
    return complete[0] if len(complete) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate2-dir", required=True)
    ap.add_argument("--output")
    args = ap.parse_args()

    g = Path(args.gate2_dir)
    e = resolve_execution(g)

    out: dict[str, Any] = {
        "schema": "blink-intent-gate2-failure-report-v2",
        "execution": e.name if e else None,
        "arms": {},
        "report_complete": False,
    }

    if e is None:
        out["diagnostic"] = "No unique completed execution could be resolved. See STRICT_ACCEPTANCE.json."
    else:
        comp = read_json(e / "comparison-summary.json")
        if comp is None:
            out["diagnostic"] = f"Missing or invalid comparison-summary.json under {e}"
        else:
            for arm in "ABC":
                cov = e / "arms" / arm / "formal" / "path-coverage.csv"
                failures = []
                if cov.is_file():
                    try:
                        with cov.open(encoding="utf-8", newline="") as fh:
                            for r in csv.DictReader(fh):
                                if r.get("phase") == "formal" and r.get("full_path_pass") == "False":
                                    raw = r.get("failure_reasons") or "[]"
                                    try:
                                        reasons = json.loads(raw)
                                    except Exception:
                                        reasons = [raw]
                                    failures.append({
                                        "request_id": r.get("request_id"),
                                        "scenario": r.get("scenario"),
                                        "vu": r.get("vu"),
                                        "failure_reasons": reasons,
                                        "truncated_generations": int(r.get("truncated_generations") or 0),
                                    })
                    except Exception as exc:
                        failures.append({"report_error": f"{type(exc).__name__}:{exc}"})

                item = (comp.get("arms") or {}).get(arm) or {}
                out["arms"][arm] = {
                    "formal_rows": item.get("formal_rows"),
                    "formal_full_path_pass": item.get("formal_full_path_pass"),
                    "formal_benchmark_pass": item.get("formal_benchmark_pass"),
                    "failure_reasons": item.get("formal_failure_reasons") or {},
                    "failed_requests": failures,
                }
            out["report_complete"] = True

    text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
