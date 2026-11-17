#!/usr/bin/env bash
# Start Mac frontend/backend and optional legacy router after GPU health passes.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/cosmetic-agent"
FRONTEND_ROOT="$PROJECT_ROOT/frontend"
ENV_FILE="$BACKEND_ROOT/.env"

die() { echo "[ERROR] $*" >&2; exit 2; }
[[ "$(uname -s)" == "Darwin" ]] || die "此腳本只啟動 Mac 端服務"
[[ -f "$ENV_FILE" ]] || die "缺少唯一後端設定檔 $ENV_FILE"
set -a
source "$ENV_FILE"
set +a
BACKEND_PYTHON="${BACKEND_PYTHON:-$BACKEND_ROOT/.venv/bin/python}"
MAC_RUNTIME_DIR="${MAC_RUNTIME_DIR:-${TMPDIR:-/tmp}/blink-cosmetic-agent}"
LOGDIR="$MAC_RUNTIME_DIR/logs"
PIDDIR="$MAC_RUNTIME_DIR/pids"
export NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next}"
[[ "$NEXT_DIST_DIR" =~ ^\.next(-[A-Za-z0-9][A-Za-z0-9_-]*)?$ ]] \
  || die "NEXT_DIST_DIR 僅允許 .next 或單層 .next-安全名稱"
FRONTEND_DIST="$FRONTEND_ROOT/$NEXT_DIST_DIR"
[[ ! -L "$FRONTEND_DIST" ]] || die "NEXT_DIST_DIR 不可為 symlink"
[[ -x "$BACKEND_PYTHON" ]] || die "找不到 Python 3.12 venv：$BACKEND_PYTHON"
"$BACKEND_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
[[ -d "$FRONTEND_ROOT/node_modules" ]] || die "frontend 尚未 npm ci"
[[ -f "$FRONTEND_DIST/BUILD_ID" ]] || die "frontend $NEXT_DIST_DIR 尚未 npm run build"
node - "$FRONTEND_DIST/routes-manifest.json" <<'JS'
const fs = require('node:fs');
const manifest = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const rewrites = Array.isArray(manifest.rewrites) ? manifest.rewrites : Object.values(manifest.rewrites).flat();
if (!rewrites.some(r => r.source === '/api/:path*' && r.destination === 'http://127.0.0.1:8010/api/:path*')) {
  console.error('[ERROR] 請先執行 ops/runtime/build-mac-frontend.sh，將 API 指向 Mac');
  process.exit(2);
}
JS
mkdir -p "$LOGDIR" "$PIDDIR"

"$SCRIPT_DIR/start-mac-redis.sh"
export LOCAL_BACKEND_OVERRIDE=1 BACKEND_API_URL=http://127.0.0.1:8010
export MEMBER_AUTH_API_BASE_URL="$BACKEND_API_URL"

workers="${BACKEND_WORKERS:-1}"
[[ "$workers" =~ ^[1-3]$ ]] || die "BACKEND_WORKERS 必須是 1、2 或 3"
if (( workers > 1 )); then
  for required in CHAT_LIMIT_REQUIRE_REDIS API_LIMIT_REQUIRE_REDIS CHAT_ADMISSION_REQUIRE_REDIS CHAT_JOB_STORE_REQUIRE_REDIS CHAT_STATE_REQUIRE_REDIS; do
    [[ "${!required:-0}" == "1" ]] || die "多 worker 必須啟用 $required=1"
  done
  export PROMETHEUS_MULTIPROC_DIR
  PROMETHEUS_MULTIPROC_DIR="$(mktemp -d "$MAC_RUNTIME_DIR/prometheus.XXXXXX")"
fi

export TAIDE_BASE_URL="${TAIDE_BASE_URL:-http://127.0.0.1:8000/v1}"
export EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-http://127.0.0.1:8002/v1}"
export RERANKER_BASE_URL="${RERANKER_BASE_URL:-http://127.0.0.1:8006/v1}"
export PHOTO_MODEL_BASE_URL="${PHOTO_MODEL_BASE_URL:-http://127.0.0.1:8025}"
export TAIDE_HEALTH_URL="${TAIDE_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
export TAIDE_ROUTER_HEALTH_URL="${TAIDE_ROUTER_HEALTH_URL:-http://127.0.0.1:8001/healthz}"
export EMBEDDING_HEALTH_URL="${EMBEDDING_HEALTH_URL:-http://127.0.0.1:8002/healthz}"
export ENABLE_TAIDE="${ENABLE_TAIDE:-0}"
[[ "$ENABLE_TAIDE" =~ ^[01]$ ]] || die "ENABLE_TAIDE 必須是 0 或 1"

model_endpoints=("$EMBEDDING_BASE_URL" "$RERANKER_BASE_URL" "$PHOTO_MODEL_BASE_URL")
[[ "$ENABLE_TAIDE" == "1" ]] && model_endpoints+=("$TAIDE_BASE_URL")
for endpoint in "${model_endpoints[@]}"; do
  case "$endpoint" in
    http://127.0.0.1:*|http://localhost:*) ;;
    *) die "模型 endpoint 必須指向 SSH 的本機 loopback，不可直連公開遠端：$endpoint" ;;
  esac
done

if [[ "$ENABLE_TAIDE" == "1" ]]; then
  curl -fsS --max-time 5 "$TAIDE_HEALTH_URL" >/dev/null \
    || die "TAIDE SSH tunnel/API 未就緒：$TAIDE_HEALTH_URL"
fi
curl -fsS --max-time 5 "$EMBEDDING_HEALTH_URL" >/dev/null \
  || die "Embedding SSH tunnel/API 未就緒：$EMBEDDING_HEALTH_URL"
if [[ "${ENABLE_RERANKER:-0}" == "1" ]]; then
  curl -fsS --max-time 5 "${RERANKER_BASE_URL%/v1}/readyz" >/dev/null \
    || die "Reranker SSH tunnel/API 未就緒"
fi
if [[ "${ENABLE_PHOTO_MODEL:-0}" == "1" ]]; then
  curl -fsS --max-time 5 "$PHOTO_MODEL_BASE_URL/health" \
    | "$BACKEND_PYTHON" -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") is True else 1)' \
    || die "Photo SSH tunnel/API 尚未載入模型"
fi

listener() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
start_bg() { # name port
  local name="$1" port="$2"
  listener "$port" && die "$name port :$port 已被未知程序占用"
  "$BACKEND_PYTHON" - "$SCRIPT_DIR/run-mac-service.sh" "$name" "$MAC_RUNTIME_DIR" "${PROMETHEUS_MULTIPROC_DIR:-}" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
entry, name, runtime, metrics = sys.argv[1:]
with (Path(runtime) / 'logs' / f'{name}.log').open('ab') as log:
    child = subprocess.Popen(
        ['/bin/bash', entry, name, runtime, metrics],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
identity = {'pid': child.pid, 'service': name,
            'start': subprocess.check_output(['ps','-p',str(child.pid),'-o','lstart='],text=True,env={**os.environ,'LC_ALL':'C'}).strip()}
(Path(runtime) / 'pids' / f'{name}.identity.json').write_text(json.dumps(identity))
PY
  printf '[START] %s port=%s log=%s\n' "$name" "$port" "$LOGDIR/$name.log"
}
wait_http() { # label url seconds
  local label="$1" url="$2" seconds="$3" elapsed=0
  while (( elapsed < seconds )); do
    curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && { printf '[READY] %s\n' "$label"; return; }
    sleep 2
    elapsed=$((elapsed + 2))
  done
  die "$label readiness timeout：$url"
}

if [[ "$ENABLE_TAIDE" == "1" ]]; then
  start_bg router 8001
  wait_http router http://127.0.0.1:8001/healthz 30
fi

start_bg backend 8010
wait_http backend http://127.0.0.1:8010/livez 120

start_bg frontend 3000
wait_http website http://127.0.0.1:3000 90
curl -fsS --max-time 10 http://127.0.0.1:8010/readyz \
  || die "Backend 進程已啟動，但依賴未就緒；請檢查 /readyz"
printf '\n[READY] Mac frontend/backend are running; GPU APIs remain reachable only through SSH loopback forwards.\n'
