#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
ROOT_OUT="${OUT:-/var/lib/jenkins/tomod/ops/8000-vs-8200-capacity/all-$STAMP}"
COMPARE_OUT="$ROOT_OUT/compare"
LIMIT_OUT="$ROOT_OUT/vllm-limit"
mkdir -p "$ROOT_OUT"

echo "===== A/B CAPACITY SWEEP ====="
OUT="$COMPARE_OUT" \
VU_LIST="${VU_LIST:-1,2,4,8,16,32,64,128,256,512,1024,2048}" \
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-30}" \
"$HERE/run_compare.sh"

echo
echo "===== vLLM LIMIT REFINEMENT ====="
OUT="$LIMIT_OUT" \
SEED_SUMMARY="$COMPARE_OUT/green_summary.json" \
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-30}" \
"$HERE/run_vllm_limit.sh"

python3 - "$ROOT_OUT" <<'PY'
import json, sys
from pathlib import Path
root=Path(sys.argv[1])
c=json.loads((root/"compare"/"comparison.json").read_text())
v=json.loads((root/"vllm-limit"/"vllm_limit.json").read_text())
obj={"comparison":c,"vllm_limit":v}
(root/"FINAL_SUMMARY.json").write_text(json.dumps(obj,ensure_ascii=False,indent=2))
md=[
"# BLINKAI :8000 vs :8200 Capacity + vLLM Limit",
"",
f"- Blue sustainable max VU: **{c.get('blue_sustainable_max_vu')}**",
f"- Green sustainable max VU from ladder: **{c.get('green_sustainable_max_vu')}**",
f"- Sustainable capacity gain: **{c.get('sustainable_capacity_gain')}×**",
f"- Peak completion-throughput gain: **{c.get('peak_completion_throughput_gain')}×**",
f"- Refined vLLM capacity statement: **{v.get('capacity_limit_statement')} VU**",
f"- vLLM peak completion throughput: **{v.get('peak_completion_tokens_per_s')} tok/s @ VU {v.get('peak_throughput_vu')}**",
"",
"vLLM limit is for the current running :8200 configuration; it is not automatically labeled the absolute MI300X hardware ceiling."
]
(root/"FINAL_REPORT.md").write_text("\n".join(md)+"\n")
print("\n".join(md))
PY

echo
echo "Final report: $ROOT_OUT/FINAL_REPORT.md"
