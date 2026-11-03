#!/usr/bin/env bash
# Stop order: backend -> router -> reranker -> embedding -> gateways -> vLLM. Databases keep running
# unless STOP_DATABASES=1.
set -Eeuo pipefail
source "$(dirname "$(readlink -f "$0")")/runtime.conf"

for name in backend router reranker embedding gateway-8003 gateway-8000 vllm-ab vllm-prod; do
  pidfile="$PIDDIR/$name.pid"
  [[ -f "$pidfile" ]] || continue
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
    echo "[STOP] $name PID=$pid"
  fi
  rm -f "$pidfile"
done

if [[ "${STOP_DATABASES:-0}" == "1" ]]; then
  redis-cli -p "$REDIS_PORT" shutdown 2>/dev/null || true
  if [[ -f "$PGDATA/PG_VERSION" ]]; then
    PG_BIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
    su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -m fast stop" || true
  fi
fi
