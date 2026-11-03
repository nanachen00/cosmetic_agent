#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; STAMP="$(date -u +%Y%m%d-%H%M%S)"; OUT="${OUT:-$HERE/results-$STAMP}"; mkdir -p "$OUT"
echo '===== BLUE :8000 ====='; python3 "$HERE/test_openai_generate.py" --url http://127.0.0.1:8000 --warmups 3 --output "$OUT/blue-8000.json"
echo; echo '===== GREEN :8100 ====='; python3 "$HERE/test_openai_generate.py" --url http://127.0.0.1:8100 --warmups 3 --output "$OUT/sglang-green.json"
echo; python3 "$HERE/compare_generation_reports.py" "$OUT/blue-8000.json" "$OUT/sglang-green.json"
echo; curl -fsS --max-time 5 http://127.0.0.1:8100/metrics | head -80 > "$OUT/sglang-metrics-head.txt" || true; cat "$OUT/sglang-metrics-head.txt" || true
echo "[OK] artifacts: $OUT"
