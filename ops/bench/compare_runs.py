#!/usr/bin/env python3
"""Merge vu_bench.py sweeps into one Markdown + CSV comparison (stdlib only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def fmt(value, digits=2):
    if value is None:
        return "—"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def main(root: str) -> None:
    root_path = Path(root)
    runs = []
    for sweep in sorted(root_path.glob("*/sweep.json")):
        data = json.loads(sweep.read_text(encoding="utf-8"))
        runs.append((sweep.parent.name, data))
    if not runs:
        raise SystemExit(f"no sweep.json under {root_path}")

    lines = [
        "# TAIDE 12B VU 量測比較",
        "",
        f"結果目錄：`{root_path}`",
        "",
        "指標：RPS = 每秒完成請求數；tok/s = 全部使用者合計的輸出 token/秒；"
        "TTFT = 首字延遲；TPOT = 每個輸出 token 的生成時間；單人 tok/s = 單一使用者感受到的生成速度。",
        "",
    ]
    rows = []
    for name, data in runs:
        meta = data["meta"]
        lines += [
            f"## {name}",
            "",
            f"input={meta['input']}，max_tokens={meta['max_tokens']}，ignore_eos={meta['ignore_eos']}，"
            f"每級 {meta['duration_s']:.0f} 秒",
            "",
            "| VU | 成功 | 錯誤率 % | RPS | tok/s | 單人 tok/s | TTFT p50 (s) | TTFT p95 (s) | TPOT p50 (ms) | TPOT p95 (ms) | 延遲 p95 (s) | VRAM max (GB) |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in data["levels"]:
            lines.append(
                f"| {s['vus']} | {s['success']} | {fmt(s['error_rate_pct'])} | {fmt(s['rps'], 3)} | "
                f"{fmt(s['output_tokens_per_s'], 1)} | {fmt(s['per_user_tokens_per_s'], 1)} | "
                f"{fmt(s['ttft_s']['p50'], 3)} | {fmt(s['ttft_s']['p95'], 3)} | "
                f"{fmt(s['tpot_ms']['p50'], 1)} | {fmt(s['tpot_ms']['p95'], 1)} | "
                f"{fmt(s['latency_s']['p95'], 2)} | {fmt(s['gpu']['vram_max_gb'], 1)} |"
            )
            rows.append({"run": name, **{k: s[k] for k in ("vus", "success", "errors", "error_rate_pct", "rps",
                                                           "output_tokens_per_s", "per_user_tokens_per_s")},
                         "ttft_p50_s": s["ttft_s"]["p50"], "ttft_p95_s": s["ttft_s"]["p95"],
                         "tpot_p50_ms": s["tpot_ms"]["p50"], "tpot_p95_ms": s["tpot_ms"]["p95"],
                         "latency_p95_s": s["latency_s"]["p95"], "vram_max_gb": s["gpu"]["vram_max_gb"]})
        best = max(data["levels"], key=lambda s: s["output_tokens_per_s"] or 0)
        lines += ["", f"峰值吞吐：VU={best['vus']}，{fmt(best['output_tokens_per_s'], 1)} tok/s，"
                      f"TTFT p95 {fmt(best['ttft_s']['p95'], 3)} s。", ""]

    # Side-by-side at each VU level
    vus_levels = sorted({s["vus"] for _, d in runs for s in d["levels"]})
    lines += ["## 各 VU 等級並列（tok/s ／ TTFT p95 s）", "",
              "| VU | " + " | ".join(name for name, _ in runs) + " |",
              "|---:|" + "---:|" * len(runs)]
    for v in vus_levels:
        cells = []
        for _, d in runs:
            s = next((x for x in d["levels"] if x["vus"] == v), None)
            cells.append(f"{fmt(s['output_tokens_per_s'], 1)} ／ {fmt(s['ttft_s']['p95'], 2)}" if s else "—")
        lines.append(f"| {v} | " + " | ".join(cells) + " |")

    (root_path / "COMPARISON.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (root_path / "comparison.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1])
