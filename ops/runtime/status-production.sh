#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FAIL=0

check() {
  if curl -fsS --max-time 4 "$2" >/dev/null 2>&1; then
    printf '  %-28s UP\n' "$1"
  else
    printf '  %-28s DOWN\n' "$1"
    FAIL=1
  fi
}
check_json_ready() {
  if curl -fsS --max-time 4 "$2" 2>/dev/null \
    | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") is True else 1)' \
    >/dev/null 2>&1; then
    printf '  %-28s UP\n' "$1"
  else
    printf '  %-28s DOWN\n' "$1"
    FAIL=1
  fi
}

check_frontend_build() {
  local selected="${NEXT_DIST_DIR:-.next}"
  local label='frontend build alignment'
  if [[ "$selected" =~ ^\.next(-[A-Za-z0-9][A-Za-z0-9_-]*)?$ ]] \
    && [[ ! -L "$PROJECT_ROOT/frontend/$selected" ]] \
    && curl -fsS --max-time 4 http://127.0.0.1:3000/api/runtime 2>/dev/null \
      | python3 -c '
import json, re, sys
from pathlib import Path
raw = sys.stdin.buffer.read(4097)
if len(raw) > 4096: raise SystemExit(1)
payload = json.loads(raw)
expected = Path(sys.argv[1]).read_text().strip()
ok = re.fullmatch(r"[A-Za-z0-9_-]{1,128}", expected) and isinstance(payload, dict) and payload.get("service") == "frontend" and payload.get("status") == "ok" and payload.get("buildId") == expected
raise SystemExit(0 if ok else 1)
' "$PROJECT_ROOT/frontend/$selected/BUILD_ID" >/dev/null 2>&1; then
    printf '  %-28s UP\n' "$label"
  else
    printf '  %-28s DOWN\n' "$label"
    FAIL=1
  fi
}

case "${BLINK_RUNTIME_ROLE:-}" in
  mac)
    if [[ -f "$PROJECT_ROOT/cosmetic-agent/.env" ]]; then
      set -a
      source "$PROJECT_ROOT/cosmetic-agent/.env"
      set +a
    fi
    echo 'BLINK Mac runtime status'
    check 'website :3000' http://127.0.0.1:3000
    check_frontend_build
    check 'backend :8010' http://127.0.0.1:8010/livez
    check 'backend readyz' http://127.0.0.1:8010/readyz
    if [[ "${ENABLE_TAIDE:-0}" == "1" ]]; then
      check 'TAIDE tunnel :8000' "${TAIDE_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
      check 'router :8001' "${TAIDE_ROUTER_HEALTH_URL:-http://127.0.0.1:8001/healthz}"
    else
      printf '  %-28s DISABLED\n' 'legacy TAIDE/router'
    fi
    check 'embedding tunnel :8002' "${EMBEDDING_HEALTH_URL:-http://127.0.0.1:8002/healthz}"
    if command -v redis-cli >/dev/null && redis-cli -h 127.0.0.1 -p 6389 ping 2>/dev/null | grep -qx PONG; then
      printf '  %-28s UP\n' 'Redis :6389'
    else
      printf '  %-28s DOWN\n' 'Redis :6389'
      FAIL=1
    fi
    [[ "${ENABLE_RERANKER:-0}" == "1" ]] && check 'reranker tunnel :8006' http://127.0.0.1:8006/readyz
    # The Mac website exposes photo analysis regardless of the GPU bootstrap
    # flag. Check its actual proxy, so a disabled startup flag cannot hide a
    # broken model tunnel from platform status.
    check_json_ready 'website photo analysis' http://127.0.0.1:3000/api/skin-photo
    ;;
  mi300x)
    source "$SCRIPT_DIR/runtime.conf"
    verify_mi300x_control_python
    "$SCRIPT_DIR/verify-mi300x-host.sh" || FAIL=1
    echo 'BLINK MI300X model runtime status'
    if [[ "$ENABLE_TAIDE" == "1" ]]; then
      check "vLLM :$VLLM_PROD_PORT" "http://127.0.0.1:$VLLM_PROD_PORT/health"
      check 'gateway :8000' http://127.0.0.1:8000/healthz
      [[ "$ENABLE_AB_MODEL" == "1" ]] && check "vLLM A/B :$VLLM_AB_PORT" "http://127.0.0.1:$VLLM_AB_PORT/health"
    else
      printf '  %-28s DISABLED\n' 'legacy TAIDE/vLLM'
    fi
    if "$MI300X_CONTROL_PYTHON" "$MI300X_BGE_CONTROLLER" status --instance bge-model; then
      printf '  %-28s UP\n' 'managed embedding :8002'
    else
      printf '  %-28s DOWN\n' 'managed embedding :8002'
      FAIL=1
    fi
    [[ "$ENABLE_RERANKER" == "1" ]] && check 'reranker :8006' http://127.0.0.1:8006/readyz
    [[ "$ENABLE_PHOTO_MODEL" == "1" ]] && check_json_ready 'photo :8025' http://127.0.0.1:8025/health
    ;;
  *)
    echo '[ERROR] 設定 BLINK_RUNTIME_ROLE=mac 或 BLINK_RUNTIME_ROLE=mi300x' >&2
    exit 2
    ;;
esac
exit "$FAIL"
