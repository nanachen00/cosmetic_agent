#!/usr/bin/env bash
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/runtime.conf"

check() {  # label url
  if curl -fsS --max-time 3 "$2" >/dev/null 2>&1; then printf '  %-28s UP\n' "$1"; else printf '  %-28s DOWN\n' "$1"; FAIL=1; fi
}
FAIL=0
echo "BLINK AI runtime status"
check "vLLM prod :$VLLM_PROD_PORT" "http://127.0.0.1:$VLLM_PROD_PORT/health"
[[ "$ENABLE_AB_MODEL" == "1" ]] && check "vLLM A/B :$VLLM_AB_PORT" "http://127.0.0.1:$VLLM_AB_PORT/health"
check "gateway :8000" "http://127.0.0.1:8000/healthz"
check "gateway :8003" "http://127.0.0.1:8003/healthz"
check "embedding :8002" "http://127.0.0.1:8002/healthz"
check "router :8001" "http://127.0.0.1:8001/healthz"
check "backend :8010" "http://127.0.0.1:8010/livez"
check "backend readyz" "http://127.0.0.1:8010/readyz"
redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 && printf '  %-28s UP\n' "redis :$REDIS_PORT" || { printf '  %-28s DOWN\n' "redis :$REDIS_PORT"; FAIL=1; }
command -v amd-smi >/dev/null && amd-smi metric -m -u 2>/dev/null | head -12
exit "$FAIL"
