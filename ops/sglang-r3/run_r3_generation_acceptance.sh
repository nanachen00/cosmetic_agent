#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/results-r3-$STAMP}"
mkdir -p "$OUT"

echo "===== BLUE :8000 ====="
python3 "$HERE/test_openai_generate.py" \
  --url http://127.0.0.1:8000 \
  --warmups 3 \
  --output "$OUT/blue-8000.json"

echo
echo "===== SGLANG GREEN :8100 ====="
python3 "$HERE/test_openai_generate.py" \
  --url http://127.0.0.1:8100 \
  --warmups 3 \
  --output "$OUT/sglang-8100.json"

echo
echo "===== SUMMARY ====="
python3 - "$OUT/blue-8000.json" "$OUT/sglang-8100.json" <<'PY'
import json, sys
from pathlib import Path
b=json.loads(Path(sys.argv[1]).read_text())
g=json.loads(Path(sys.argv[2]).read_text())
bm={x["name"]:x for x in b["results"]}
gm={x["name"]:x for x in g["results"]}
print("| Case | Blue | Green | Blue s | Green s | Exact text |")
print("|---|---:|---:|---:|---:|---:|")
bad=False
for name in sorted(set(bm)|set(gm)):
    x,y=bm[name],gm[name]
    bad |= not x["ok"] or not y["ok"]
    print(f"| {name} | {'PASS' if x['ok'] else 'FAIL'} | {'PASS' if y['ok'] else 'FAIL'} | {x['latency_s']} | {y['latency_s']} | {'yes' if x['content']==y['content'] else 'no'} |")
raise SystemExit(1 if bad else 0)
PY

echo
echo "[OK] R3 generation acceptance PASS"
echo "Artifacts: $OUT"
