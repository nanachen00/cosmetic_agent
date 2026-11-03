#!/usr/bin/env bash
# Engine check + TAIDE 12B VU benchmark for vLLM and SGLang (added 2026-10-22).
# Needs only the downloaded model and the compiled /opt/venv-llm — no .env, DB or API keys.
#
#   tmux new -s bench
#   bash /var/lib/jenkins/tomod/ops/bench/run_engine_bench.sh
#
# Knobs (environment):
#   RUNS="vllm:compat vllm:perf sglang:default"   engine:profile list, run in order
#         vllm:compat = --enforce-eager (the profile the team validated)  vllm:perf = HIP graphs
#         sglang:default = CUDA/HIP graphs on        sglang:compat = --disable-cuda-graph
#   VU_LIST=1,2,4,8,16,32,64   DURATION=60 (s per level)   MAX_TOKENS=256
#   INPUT=short|long   (long = ~1.5k-token RAG-style prompt)   IGNORE_EOS=0|1
#   BENCH_MEM=0.85  BENCH_MAX_SEQS=128  BENCH_MAX_MODEL_LEN=32768  SGLANG_ATTN=triton
#   MODEL_PATH=/data/blink/models/taide-12b  (use .../taide-12b-2602 for the A/B model)
set -Eeuo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
source "$HERE/../runtime/runtime.conf"

RUNS="${RUNS:-vllm:compat vllm:perf sglang:default}"
VU_LIST="${VU_LIST:-1,2,4,8,16,32,64}"
DURATION="${DURATION:-60}"
MAX_TOKENS="${MAX_TOKENS:-256}"
INPUT="${INPUT:-short}"
IGNORE_EOS="${IGNORE_EOS:-0}"
BENCH_MEM="${BENCH_MEM:-0.85}"
BENCH_MAX_SEQS="${BENCH_MAX_SEQS:-128}"
BENCH_MAX_MODEL_LEN="${BENCH_MAX_MODEL_LEN:-32768}"
SGLANG_ATTN="${SGLANG_ATTN:-triton}"
MODEL_PATH="${MODEL_PATH:-$PROD_MODEL_PATH}"
PORT="${BENCH_PORT:-8290}"
PY="$VLLM_ENV/bin/python"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BENCH_OUT:-$ROOT/ops/bench/results/$STAMP}"
mkdir -p "$OUT"
exec > >(tee -a "$OUT/bench.log") 2>&1

[[ -f "$MODEL_PATH/config.json" ]] || die "找不到模型 $MODEL_PATH"
for p in "$VLLM_PROD_PORT" "$VLLM_AB_PORT" 8000; do
  listener "$p" && die "偵測到正式服務仍在執行（:$p）。量測需要整張 GPU，請先執行 stop-production.sh"
done
listener "$PORT" && die ":$PORT 已被占用，可用 BENCH_PORT 指定其他埠"

TORCH_LIB="$("$PY" -c 'import torch,pathlib;print(pathlib.Path(torch.__file__).parent/"lib")' 2>/dev/null | tail -1)"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}" VLLM_TARGET_DEVICE=rocm

log "1. 編譯檢查（vLLM / SGLang）"
"$PY" "$HERE/engine_check.py" | tee "$OUT/engine_check.txt" || echo "[WARN] 有檢查未通過，對應引擎的量測可能失敗"
VLLM_OK=1; SGLANG_OK=1
grep -q "^\[FAIL\] vLLM" "$OUT/engine_check.txt" && VLLM_OK=0
grep -q "^\[FAIL\] SGLang\|^\[FAIL\] sgl_kernel" "$OUT/engine_check.txt" && SGLANG_OK=0

server_cmd() {  # engine profile -> one arg per line
  local engine="$1" profile="$2" args
  if [[ "$engine" == "vllm" ]]; then
    args=("$PY" -m vllm.entrypoints.openai.api_server --model "$MODEL_PATH" --served-model-name taide-12b
      --host 127.0.0.1 --port "$PORT" --dtype bfloat16 --max-model-len "$BENCH_MAX_MODEL_LEN"
      --gpu-memory-utilization "$BENCH_MEM" --max-num-seqs "$BENCH_MAX_SEQS"
      --generation-config auto --disable-log-requests --limit-mm-per-prompt "image=0")
    [[ "$profile" == "compat" ]] && args+=(--enforce-eager)
  else
    args=("$PY" -m sglang.launch_server --model-path "$MODEL_PATH" --served-model-name taide-12b
      --host 127.0.0.1 --port "$PORT" --dtype bfloat16 --context-length "$BENCH_MAX_MODEL_LEN"
      --mem-fraction-static "$BENCH_MEM" --max-running-requests "$BENCH_MAX_SEQS"
      --attention-backend "$SGLANG_ATTN")
    [[ "$profile" == "compat" ]] && args+=(--disable-cuda-graph)
  fi
  printf '%s\n' "${args[@]}"
}

stop_server() {
  local pid="$1"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null || true
  pkill -f "port $PORT" 2>/dev/null || true   # engine worker subprocesses
  sleep 15                                    # let the GPU release memory
}

SUMMARY=()
for run in $RUNS; do
  engine="${run%%:*}"; profile="${run#*:}"; name="${engine}-${profile}"
  if [[ "$engine" == "vllm" && "$VLLM_OK" == "0" ]] || [[ "$engine" == "sglang" && "$SGLANG_OK" == "0" ]]; then
    SUMMARY+=("$name: SKIPPED（編譯檢查未通過）"); continue
  fi
  log "2. $name：啟動 TAIDE 12B（:$PORT，首次載入約 2–6 分鐘）"
  mkdir -p "$OUT/$name"
  mapfile -t CMD < <(server_cmd "$engine" "$profile")
  printf '%q ' "${CMD[@]}" > "$OUT/$name/command.txt"
  "${CMD[@]}" > "$OUT/$name/server.log" 2>&1 &
  SERVER_PID=$!
  ready=0
  for _ in $(seq 1 450); do
    kill -0 "$SERVER_PID" 2>/dev/null || break
    curl -fsS --max-time 3 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && { ready=1; break; }
    sleep 2
  done
  if [[ "$ready" != "1" ]]; then
    echo "[FAIL] $name 啟動失敗，server.log 最後 40 行："; tail -40 "$OUT/$name/server.log" || true
    SUMMARY+=("$name: 啟動失敗（見 $OUT/$name/server.log）")
    stop_server "$SERVER_PID"; continue
  fi
  echo "[READY] $name，載入耗時與記憶體見 server.log"

  log "3. $name：smoke 測試"
  curl -sS --max-time 120 "http://127.0.0.1:$PORT/v1/chat/completions" -H 'Content-Type: application/json' \
    -d '{"model":"taide-12b","messages":[{"role":"user","content":"請用兩句繁體中文說明玻尿酸的保濕原理。"}],"max_tokens":120,"temperature":0}' \
    | tee "$OUT/$name/smoke.json" | "$PY" -c 'import sys,json;print("回覆：",json.load(sys.stdin)["choices"][0]["message"]["content"])' \
    || echo "[WARN] smoke 回覆解析失敗，見 smoke.json"

  log "4. $name：VU 量測 VU=$VU_LIST，每級 ${DURATION}s，input=$INPUT，max_tokens=$MAX_TOKENS"
  EXTRA=(); [[ "$IGNORE_EOS" == "1" ]] && EXTRA+=(--ignore-eos)
  if python3 "$HERE/vu_bench.py" --url "http://127.0.0.1:$PORT" --model taide-12b --label "$name" \
       --vu-list "$VU_LIST" --duration "$DURATION" --max-tokens "$MAX_TOKENS" --input "$INPUT" \
       "${EXTRA[@]}" --output-dir "$OUT/$name"; then
    SUMMARY+=("$name: 完成，無錯誤")
  else
    SUMMARY+=("$name: 完成，但有請求錯誤（見 $OUT/$name/vu*/summary.json）")
  fi
  stop_server "$SERVER_PID"
done

log "5. 比較報告"
python3 "$HERE/compare_runs.py" "$OUT" > /dev/null 2>&1 && echo "報告：$OUT/COMPARISON.md" || echo "[WARN] 沒有可比較的結果"
printf '%s\n' "${SUMMARY[@]}"
tar -C "$(dirname "$OUT")" -czf "$OUT.tar.gz" "$(basename "$OUT")"
echo
echo "結果打包：$OUT.tar.gz"
echo "容器重建時這些結果會消失，請下載保存（例如在 host 上：docker cp <容器>:$OUT.tar.gz .）"
