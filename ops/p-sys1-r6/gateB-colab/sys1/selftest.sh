#!/usr/bin/env bash
# 離線自測：假 Kev(127.0.0.1:8239) + 臨時 registry；不碰 GPU/production/真模型。在 gateB-colab 目錄下執行：bash sys1/selftest.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"; T="$(mktemp -d)"; trap 'kill $MP 2>/dev/null || true; rm -rf "$T" sys1/frozen/selftest-*.json' EXIT
python3 vendor_fk/tests/mock_kev.py --port 8239 >/dev/null 2>&1 & MP=$!; sleep 2
cat >"$T/reg.json" <<J
{"candidates":[{"id":"selftest-ok","role":"candidate","lineage":["ModernBERT-large"],"base_origin":"US","provenance_verified":true,"verified_by":"selftest","verified_on":"-","model_field":"m","kev_label":"selftest-ok"},
{"id":"selftest-qwen","role":"candidate","lineage":["Qwen3-0.6B"],"base_origin":"CN","provenance_verified":true,"verified_by":"t","verified_on":"-","model_field":"m","kev_label":"selftest-qwen"},
{"id":"selftest-unv","role":"candidate","lineage":["X"],"base_origin":"US","provenance_verified":false,"model_field":"m","kev_label":"selftest-unv"}]}
J
export SYS1_REGISTRY="$T/reg.json"
python3 sys1/policy.py check selftest-ok; ! python3 sys1/policy.py check selftest-qwen; ! python3 sys1/policy.py check selftest-unv
python3 sys1/run_cand.py probe --cand selftest-ok --kev-url http://127.0.0.1:8239
python3 sys1/run_cand.py collect --cand selftest-ok --run "$T/gd" --split all --kev-url http://127.0.0.1:8239 --vus 4 >/dev/null
python3 sys1/run_cand.py collect --cand selftest-ok --run "$T/gold" --dataset router_gold --kev-url http://127.0.0.1:8239 --vus 4 >/dev/null
python3 sys1/analyze_cand.py --cand selftest-ok --cal-run "$T/gd" --hold-run "$T/gold" >/dev/null || [ $? -eq 5 ]   # mock 不是模型，FAIL(5) 是正常的
test -s "$T/gold/sys1_selftest-ok/SYS1_HOLDOUT.md"; echo "sys1 selftest OK（mock 只驗證接線；數字沒有意義）"
