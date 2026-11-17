#!/usr/bin/env bash
# Start only loopback-bound model services on the verified MI300X host.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/runtime.conf"
[[ "$ENABLE_TAIDE" =~ ^[01]$ ]] || die "ENABLE_TAIDE 必須是 0 或 1"
verify_mi300x_control_python
"$SCRIPT_DIR/verify-mi300x-host.sh"
"$SCRIPT_DIR/verify-mi300x-mount.sh"

[[ "$ENABLE_AB_MODEL" != "1" || "$ENABLE_TAIDE" == "1" ]] || die "ENABLE_AB_MODEL 需要明確啟用 ENABLE_TAIDE=1"
required_models="embedding"
[[ "$ENABLE_TAIDE" == "1" ]] && required_models+=",taide-prod"
[[ "$ENABLE_AB_MODEL" == "1" ]] && required_models+=",taide-ab"
[[ "$ENABLE_RERANKER" == "1" ]] && required_models+=",reranker"
[[ "$ENABLE_PHOTO_MODEL" == "1" ]] && required_models+=",photo-base"
python3 "$SCRIPT_DIR/verify-model-artifacts.py" --models "$required_models"

mkdir -p "$LOGDIR" "$PIDDIR"
VPY="$VLLM_ENV/bin/python"
if [[ "$ENABLE_TAIDE" == "1" ]]; then
  [[ -x "$VPY" ]] || die "找不到 vLLM Python：$VPY"
fi

listener() { ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .; }
start_bg() { # name port logfile command...
  local name="$1" port="$2" logfile="$3"; shift 3
  if listener "$port"; then
    die "$name 預定的 loopback port :$port 已被占用；不接管未知程序"
  fi
  nohup "$@" >"$logfile" 2>&1 &
  printf '%s\n' "$!" >"$PIDDIR/$name.pid"
  printf '[START] %s port=%s pid=%s log=%s\n' "$name" "$port" "$!" "$logfile"
}
wait_http() { # label url seconds pid-name
  local label="$1" url="$2" seconds="$3" pid_name="$4"
  local elapsed=0
  while (( elapsed < seconds )); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      printf '[READY] %s\n' "$label"
      return
    fi
    if ! kill -0 "$(<"$PIDDIR/$pid_name.pid")" 2>/dev/null; then
      tail -n 80 "$LOGDIR/$pid_name.log" || true
      die "$label 啟動中止"
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  die "$label readiness timeout：$url"
}
wait_json_ready() { # label url seconds pid-name
  local label="$1" url="$2" seconds="$3" pid_name="$4"
  local elapsed=0
  while (( elapsed < seconds )); do
    if curl -fsS --max-time 3 "$url" 2>/dev/null \
      | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") is True else 1)' \
      >/dev/null 2>&1; then
      printf '[READY] %s\n' "$label"
      return
    fi
    if ! kill -0 "$(<"$PIDDIR/$pid_name.pid")" 2>/dev/null; then
      tail -n 80 "$LOGDIR/$pid_name.log" || true
      die "$label 啟動中止"
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  die "$label readiness timeout：$url"
}

export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}" HF_HOME
# Each environment resolves its own Torch library before importing Torch.
torch_library() {
  env -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 \
    "$1" -c 'import pathlib,importlib.util; print(pathlib.Path(importlib.util.find_spec("torch").origin).parent / "lib")'
}

engine_cmd() { # path served port memory
  local model="$1" served="$2" port="$3" memory="$4"
  local multimodal_limit='{"image":0}'
  local -a args
  if [[ "$ENGINE" == "sglang" ]]; then
    args=("$VPY" -m sglang.launch_server --model-path "$model" --served-model-name "$served"
      --host 127.0.0.1 --port "$port" --dtype bfloat16 --context-length "$VLLM_MAX_MODEL_LEN"
      --mem-fraction-static "$memory" --max-running-requests "$VLLM_MAX_NUM_SEQS")
  else
    # vLLM 0.8.x used the legacy image=0 syntax; 0.9+ accepts JSON.
    if "$VPY" -c 'import sys,vllm; from packaging.version import Version; sys.exit(0 if Version(vllm.__version__) < Version("0.9") else 1)' 2>/dev/null; then
      multimodal_limit='image=0'
    fi
    args=("$VPY" -m vllm.entrypoints.openai.api_server --model "$model" --served-model-name "$served"
      --host 127.0.0.1 --port "$port" --dtype bfloat16 --max-model-len "$VLLM_MAX_MODEL_LEN"
      --gpu-memory-utilization "$memory" --max-num-seqs "$VLLM_MAX_NUM_SEQS"
      --generation-config auto --disable-log-requests --limit-mm-per-prompt "$multimodal_limit")
    [[ "$VLLM_PROFILE" == "compat" ]] && args+=(--enforce-eager)
  fi
  printf '%s\n' "${args[@]}"
}

if [[ "$ENABLE_TAIDE" == "1" ]]; then
VLLM_TORCH_LIB="$(torch_library "$VPY")"
export LD_LIBRARY_PATH="$VLLM_TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64" VLLM_TARGET_DEVICE=rocm
mapfile -t prod_cmd < <(engine_cmd "$PROD_MODEL_PATH" taide-12b "$VLLM_PROD_PORT" "$VLLM_PROD_MEM")
start_bg vllm-prod "$VLLM_PROD_PORT" "$LOGDIR/vllm-prod.log" "${prod_cmd[@]}"
wait_http "vLLM production" "http://127.0.0.1:$VLLM_PROD_PORT/v1/models" 600 vllm-prod

upstreams="taide-12b=http://127.0.0.1:$VLLM_PROD_PORT"
if [[ "$ENABLE_AB_MODEL" == "1" ]]; then
  mapfile -t ab_cmd < <(engine_cmd "$AB_MODEL_PATH" taide-12b-2602 "$VLLM_AB_PORT" "$VLLM_AB_MEM")
  start_bg vllm-ab "$VLLM_AB_PORT" "$LOGDIR/vllm-ab.log" "${ab_cmd[@]}"
  wait_http "vLLM A/B" "http://127.0.0.1:$VLLM_AB_PORT/v1/models" 600 vllm-ab
  upstreams+=",taide-12b-2602=http://127.0.0.1:$VLLM_AB_PORT"
fi

start_bg gateway-8000 8000 "$LOGDIR/gateway-8000.log" \
  env TAIDE_UPSTREAMS="$upstreams" TAIDE_BIND_HOST=127.0.0.1 \
  "$VPY" "$SCRIPT_DIR/taide_vllm_gateway.py" --host 127.0.0.1 --port 8000
wait_http "TAIDE gateway" http://127.0.0.1:8000/healthz 90 gateway-8000
fi

# Formal BGE uses the durable secure active pointer. No legacy8002 takeover,
# activation, raw-PID creation, or HTTP200-only readiness occurs at cold start.
"$MI300X_CONTROL_PYTHON" "$MI300X_BGE_CONTROLLER" start --instance bge-model --wait 300

if [[ "$ENABLE_RERANKER" == "1" ]]; then
  [[ -n "${RERANKER_ENV:-}" && -x "$RERANKER_ENV/bin/python" ]] || die "reranker 需獨立且已驗證的 RERANKER_ENV；不共用 BGE 環境"
  RPY="$RERANKER_ENV/bin/python"
  RERANK_TORCH_LIB="$(torch_library "$RPY")"
  start_bg reranker 8006 "$LOGDIR/reranker.log" \
    env -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD PYTHONNOUSERSITE=1 \
    LD_LIBRARY_PATH="$RERANK_TORCH_LIB:/opt/rocm/lib:/opt/rocm/lib64" \
    HF_HOME="$HF_HOME" RERANKER_MODEL_NAME="$RERANKER_MODEL_PATH" \
    RERANKER_DEVICE="${RERANKER_DEVICE:-cuda}" RERANKER_HOST=127.0.0.1 \
    "$RPY" -m uvicorn --app-dir "$PROJECT_ROOT/cosmetic-agent" api_server_reranker:app \
    --host 127.0.0.1 --port 8006
  wait_http "BGE reranker" http://127.0.0.1:8006/readyz 300 reranker
fi

if [[ "$ENABLE_PHOTO_MODEL" == "1" ]]; then
  [[ -z "$PHOTO_ADAPTER_PATH" ]] || die "研究 adapter 尚未通過照片功能驗收；不自動部署"
  photo_args=()
  [[ -z "$PHOTO_ENV" ]] || photo_args+=(--python "$PHOTO_ENV/bin/python")
  [[ -z "$PHOTO_SERVICE_SOURCE" ]] || photo_args+=(--source "$PHOTO_SERVICE_SOURCE")
  "$MI300X_CONTROL_PYTHON" "$MI300X_PHOTO_CONTROLLER" start "${photo_args[@]}" --wait 600
fi

printf '\n[READY] MI300X APIs are bound to 127.0.0.1 only; connect through SSH forwarding.\n'
