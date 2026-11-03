#!/usr/bin/env bash
# T4 端到端接線自測：產資料 → 訓練 → serve → run_cand probe → collect(gate_data all + router_gold) → analyze_cand。
# 用臨時 registry（selftest-train-*，已「簽核」只為了讓 policy 放行接線；不是候選）。不碰 GPU/production/真模型下載。
# 在 gateB-colab 目錄執行：bash sys1/train/selftest_train.sh [np-tiny] [tiny-random-bert]
#   預設跑 np-tiny；有 torch/transformers 時再加跑 tiny-random-bert（torch 路徑）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$HERE"
T="$(mktemp -d)"; SP=""
cleanup() { [ -n "$SP" ] && kill "$SP" 2>/dev/null || true; rm -rf "$T" sys1/frozen/selftest-train-*.json; }
trap cleanup EXIT
BASES=("$@")
if [ ${#BASES[@]} -eq 0 ]; then
  BASES=(np-tiny)
  python3 -c "import torch, transformers" 2>/dev/null && BASES+=(tiny-random-bert) || echo "[selftest-train] 沒有 torch/transformers：只跑 np-tiny"
fi
PORT=8233
echo "[1] make_train_data"; python3 -I sys1/train/make_train_data.py --out "$T/data" --per 60 >/dev/null
for B in "${BASES[@]}"; do
  ID="selftest-train-$B"; NAME="$B-selftest"
  cat >"$T/reg.json" <<J
{"candidates":[{"id":"$ID","role":"candidate","lineage":["$B (wiring only)"],"base_origin":"US","provenance_verified":true,"verified_by":"selftest","verified_on":"-","model_field":"$NAME","kev_label":"$ID"}]}
J
  export SYS1_REGISTRY="$T/reg.json"
  echo "[2] train $B"; python3 -I sys1/train/train_head.py --base "$B" --data "$T/data" --seed 13 --out "$T/ck-$B" --device cpu >"$T/train-$B.log" 2>&1 || { tail -20 "$T/train-$B.log"; exit 1; }
  echo "[3] serve $B :$PORT"; python3 -I sys1/train/serve_sys1.py --ckpt "$T/ck-$B" --name "$NAME" --port $PORT --device cpu >"$T/serve-$B.log" 2>&1 & SP=$!
  for i in $(seq 1 120); do curl -sf "127.0.0.1:$PORT/v1/models" >/dev/null && break; sleep 0.5; done
  echo "[4] probe";   python3 sys1/run_cand.py probe --cand "$ID" --kev-url "http://127.0.0.1:$PORT"
  echo "[5] collect gate_data (all)"; python3 sys1/run_cand.py collect --cand "$ID" --run "$T/gd-$B" --split all --kev-url "http://127.0.0.1:$PORT" --vus 4 >/dev/null
  echo "[6] collect router_gold";    python3 sys1/run_cand.py collect --cand "$ID" --run "$T/gold-$B" --dataset router_gold --kev-url "http://127.0.0.1:$PORT" --vus 4 >/dev/null
  n=$(wc -l < "$T/gold-$B/kev/$ID/raw.jsonl"); [ "$n" -ge 300 ] || { echo "router_gold raw rows=$n"; exit 1; }
  bad=$(python3 -c "import json,sys;print(sum(1 for l in open(sys.argv[1]) if json.loads(l)['status']!=200))" "$T/gold-$B/kev/$ID/raw.jsonl"); [ "$bad" -eq 0 ] || { echo "non-200 rows: $bad"; exit 1; }
  echo "[7] analyze (+ Kev-0.8B gate_data 對照)"; python3 sys1/analyze_cand.py --cand "$ID" --cal-run "$T/gd-$B" --hold-run "$T/gold-$B" --kev-ref-run sys1/reference/gateB-t4-001 >/dev/null || [ $? -eq 5 ]
  test -s "$T/gold-$B/sys1_$ID/SYS1_HOLDOUT.md"; test -s "sys1/frozen/$ID.json"
  grep -q "Kev-0.8B（Colab，凍結 v2 門檻）|" "$T/gold-$B/sys1_$ID/SYS1_HOLDOUT.md" || { echo "Kev reference section missing"; exit 1; }
  (cd sys1/reference/gateB-t4-001 && sha256sum -c --quiet SHA256SUMS)
  curl -sf "127.0.0.1:$PORT/v1/stats"; echo
  kill $SP; wait $SP 2>/dev/null || true; SP=""
done
python3 holdout.py | grep -q "FROZEN ✓"
echo "selftest-train OK（${BASES[*]}；tiny 模型只驗證接線，數字沒有意義）"
