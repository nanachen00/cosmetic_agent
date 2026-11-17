#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REDIS_SERVER="${REDIS_SERVER:-$(command -v redis-server || true)}"
REDIS_CLI="${REDIS_CLI:-$(command -v redis-cli || true)}"
[[ -n "$REDIS_SERVER" && -n "$REDIS_CLI" ]] || {
  echo '[ERROR] Redis 尚未安裝：brew install redis' >&2; exit 2;
}
if lsof -nP -iTCP:6389 -sTCP:LISTEN >/dev/null 2>&1; then
  "$REDIS_CLI" -h 127.0.0.1 -p 6389 ping | grep -qx PONG || exit 2
  echo '[OK] Existing Redis :6389 responds PONG'
  exit 0
fi
REDIS_DATA_DIR="$PROJECT_ROOT/.runtime/redis"
mkdir -p "$REDIS_DATA_DIR"
chmod 700 "$PROJECT_ROOT/.runtime" "$REDIS_DATA_DIR"
"$REDIS_SERVER" --bind 127.0.0.1 --port 6389 --protected-mode yes \
  --dir "$REDIS_DATA_DIR" --appendonly yes --appendfsync everysec \
  --maxmemory 256mb --maxmemory-policy noeviction --daemonize yes \
  --pidfile "$REDIS_DATA_DIR/redis.pid" --logfile "$REDIS_DATA_DIR/redis.log"
"$REDIS_CLI" -h 127.0.0.1 -p 6389 ping | grep -qx PONG
echo '[OK] Redis :6389 ready (loopback, AOF, noeviction)'
