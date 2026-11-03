#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${OUT:-$HERE/results-vllm-v092-$STAMP}"
mkdir -p "$OUT"

echo "===== BLUE :8000 ====="
python3 "$HERE/test_openai_generate.py" \
  --url http://127.0.0.1:8000 \
  --warmups 3 \
  --output "$OUT/blue-8000.json"

echo
echo "===== vLLM :8200 ====="
python3 "$HERE/test_openai_generate.py" \
  --url http://127.0.0.1:8200 \
  --warmups 3 \
  --output "$OUT/vllm-8200.json"

python3 - "$OUT/blue-8000.json" "$OUT/vllm-8200.json" <<'PY'
import json, sys
from pathlib import Path
b=json.loads(Path(sys.argv[1]).read_text())
v=json.loads(Path(sys.argv[2]).read_text())
bm={x["name"]:x for x in b["results"]}
vm={x["name"]:x for x in v["results"]}
print("| Case | Blue | vLLM | Blue s | vLLM s | Exact text |")
print("|---|---:|---:|---:|---:|---:|")
bad=False
for name in sorted(set(bm)|set(vm)):
    x,y=bm[name],vm[name]
    bad |= not x["ok"] or not y["ok"]
    print(f"| {name} | {'PASS' if x['ok'] else 'FAIL'} | {'PASS' if y['ok'] else 'FAIL'} | {x['latency_s']} | {y['latency_s']} | {'yes' if x['content']==y['content'] else 'no'} |")
raise SystemExit(1 if bad else 0)
PY

echo
echo "[OK] generation acceptance artifacts: $OUT"
