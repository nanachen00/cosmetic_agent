#!/usr/bin/env bash
# Start order: PostgreSQL/Redis -> vLLM prod (8200) [-> vLLM A/B (8201)] -> gateway 8000/8003
#              -> embedding 8002 -> router 8001 -> backend 8010 -> /readyz
# The old HF Transformers server (taide_server_graphrag.py) is no longer started.
set -Eeuo pipefail
source "$(dirname "$(readlink -f "$0")")/runtime.conf"
mkdir -p "$LOGDIR" "$PIDDIR"
ENV_FILE="$ROOT/cosmetic-agent/.env"
[[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE（由 .env.example 複製並填寫）"

start_bg() {  # name port logfile cmd...
  local name="$1" port="$2" logfile="$3"; shift 3
  if listener "$port"; then echo "[UP] $name :$port 已在執行"; return; fi
  nohup "$@" >"$logfile" 2>&1 &
  echo $! > "$PIDDIR/$name.pid"
  echo "[START] $name :$port PID=$! log=$logfile"
}

wait_http() {  # name url timeout_seconds pidname
  local name="$1" url="$2" timeout="$3" pidname="${4:-}"
  for ((i = 0; i < timeout; i += 2)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then echo "[READY] $name"; return 0; fi
    if [[ -n "$pidname" && -f "$PIDDIR/$pidname.pid" ]] && ! kill -0 "$(cat "$PIDDIR/$pidname.pid")" 2>/dev/null; then
      echo "[ERROR] $name 啟動中途結束，log 最後 60 行："; tail -60 "$LOGDIR/$pidname.log" || true; exit 3
    fi
    sleep 2
  done
  echo "[ERROR] 等待 $name 逾時（$url）"; exit 4
}

# ── data stores ───────────────────────────────────────────────────
log "PostgreSQL / Redis"
if [[ -z "${EXTERNAL_DATABASE_URL:-}" && -f "$PGDATA/PG_VERSION" ]]; then
  PG_BIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
  su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' status" >/dev/null 2>&1 \
    || su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -l '$PGDATA/server.log' -w start"
fi
redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 || redis-server --port "$REDIS_PORT" --bind 127.0.0.1 \
  --dir "$REDIS_DIR" --appendonly yes --daemonize yes --logfile "$REDIS_DIR/redis.log"

# ── vLLM ──────────────────────────────────────────────────────────
VPY="$VLLM_ENV/bin/python"
TORCH_LIB="$("$VPY" -c 'import torch,pathlib;print(pathlib.Path(torch.__file__).parent/"lib")')"
export LD_LIBRARY_PATH="$TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}" VLLM_TARGET_DEVICE=rocm

VLLM_VERSION="$("$VPY" -c 'import vllm;print(vllm.__version__)' 2>/dev/null | tail -n 1)"
if [[ ! "$VLLM_VERSION" =~ ^[0-9]+([.][0-9]+)* ]]; then
  VLLM_VERSION="0.0"
fi

engine_cmd() {  # model_path served_name port mem  -> one argument per line
  local model="$1" served="$2" port="$3" mem="$4" args
  if [[ "$ENGINE" == "sglang" ]]; then
    args=("$VPY" -m sglang.launch_server --model-path "$model" --served-model-name "$served"
      --host 127.0.0.1 --port "$port" --dtype bfloat16 --context-length "$VLLM_MAX_MODEL_LEN"
      --mem-fraction-static "$mem" --max-running-requests "$VLLM_MAX_NUM_SEQS")
  else
    args=("$VPY" -m vllm.entrypoints.openai.api_server --model "$model" --served-model-name "$served"
      --host 127.0.0.1 --port "$port" --dtype bfloat16 --max-model-len "$VLLM_MAX_MODEL_LEN"
      --gpu-memory-utilization "$mem" --max-num-seqs "$VLLM_MAX_NUM_SEQS"
      --generation-config auto --disable-log-requests)
    # Gemma 3 TAIDE is multimodal-capable; serve text only. vLLM < 0.9 uses key=value syntax.
    if grep -q '"Gemma3ForConditionalGeneration"' "$model/config.json" 2>/dev/null; then
      if "$VPY" -c "import sys;v=tuple(int(x) for x in '$VLLM_VERSION'.split('.')[:2]);sys.exit(0 if v>=(0,9) else 1)"; then
        args+=(--limit-mm-per-prompt '{"image":0}')
      else
        args+=(--limit-mm-per-prompt "image=0")
      fi
    fi
    [[ "$VLLM_PROFILE" == "compat" ]] && args+=(--enforce-eager)
  fi
  [[ -n "${VLLM_EXTRA_ARGS:-}" ]] && read -r -a extra <<<"$VLLM_EXTRA_ARGS" && args+=("${extra[@]}")
  printf '%s\n' "${args[@]}"
}

log "$ENGINE 正式模型 taide-12b :$VLLM_PROD_PORT（$PROD_MODEL_PATH）"
[[ -f "$PROD_MODEL_PATH/config.json" ]] || die "找不到模型，先執行 install/50_download_models.sh"
mapfile -t CMD < <(engine_cmd "$PROD_MODEL_PATH" taide-12b "$VLLM_PROD_PORT" "$VLLM_PROD_MEM")
start_bg vllm-prod "$VLLM_PROD_PORT" "$LOGDIR/vllm-prod.log" "${CMD[@]}"
wait_http "$ENGINE prod" "http://127.0.0.1:$VLLM_PROD_PORT/v1/models" 600 vllm-prod

UPSTREAMS="taide-12b=http://127.0.0.1:$VLLM_PROD_PORT"
if [[ "$ENABLE_AB_MODEL" == "1" ]]; then
  log "$ENGINE A/B 模型 taide-12b-2602 :$VLLM_AB_PORT"
  mapfile -t CMD < <(engine_cmd "$AB_MODEL_PATH" taide-12b-2602 "$VLLM_AB_PORT" "$VLLM_AB_MEM")
  start_bg vllm-ab "$VLLM_AB_PORT" "$LOGDIR/vllm-ab.log" "${CMD[@]}"
  wait_http "$ENGINE A/B" "http://127.0.0.1:$VLLM_AB_PORT/v1/models" 600 vllm-ab
  UPSTREAMS="$UPSTREAMS,taide-12b-2602=http://127.0.0.1:$VLLM_AB_PORT"
fi

log "相容閘道 :8000（取代 HF Transformers 版 TAIDE）與 :8003（取代 LiteLLM）"
for port in 8000 8003; do
  TAIDE_UPSTREAMS="$UPSTREAMS" start_bg "gateway-$port" "$port" "$LOGDIR/gateway-$port.log" \
    "$VPY" "$ROOT/ops/runtime/taide_vllm_gateway.py" --port "$port"
done
wait_http "gateway :8000" "http://127.0.0.1:8000/healthz" 60 gateway-8000

# ── embedding / router / backend ─────────────────────────────────
log "BGE-M3 embedding :8002"
EMBEDDING_DEVICE="${EMBEDDING_DEVICE:-cuda}" HF_HOME="$HF_HOME" \
  start_bg embedding 8002 "$LOGDIR/embedding.log" "$EMBED_ENV/bin/python" "$ROOT/api_server_embedding.py"
wait_http "embedding" "http://127.0.0.1:8002/healthz" 240 embedding

if [[ "${ENABLE_RERANKER:-0}" == "1" ]]; then
  log "Reranker :8006"
  (cd "$ROOT/cosmetic-agent" && HF_HOME="$HF_HOME" start_bg reranker 8006 "$LOGDIR/reranker.log" \
    "$EMBED_ENV/bin/python" -m uvicorn api_server_reranker:app --host 127.0.0.1 --port 8006)
fi

log "TAIDE router :8001"
start_bg router 8001 "$LOGDIR/router.log" "$BACKEND_ENV/bin/python" "$ROOT/taide_router_8001.py"
wait_http "router" "http://127.0.0.1:8001/healthz" 60 router

log "Backend :8010"
(
  cd "$ROOT/cosmetic-agent"
  set -a; source "$ENV_FILE"; set +a
  export PYTHONUNBUFFERED=1 PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-$ROOT/ops/runtime/prometheus}"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  start_bg backend 8010 "$LOGDIR/backend.log" "$BACKEND_ENV/bin/python" -m uvicorn api.api_server:app \
    --host 127.0.0.1 --port 8010 --workers "${BACKEND_WORKERS:-1}"
)
wait_http "backend" "http://127.0.0.1:8010/livez" 120 backend

log "就緒檢查"
curl -sS --max-time 10 http://127.0.0.1:8010/readyz | python3 -m json.tool || true
"$ROOT/ops/runtime/status-production.sh" || true
