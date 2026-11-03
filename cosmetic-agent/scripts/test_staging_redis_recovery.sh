#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/var/lib/jenkins/tomod"
CONFIG="$ROOT/cosmetic-agent/deploy/staging/redis.conf"
STATE_DIR="$ROOT/ops/staging/redis"
DATA_DIR="$STATE_DIR/data"
PORT=6389
KEY_PREFIX="blink:staging:restore:$$:$(date +%s)"

mkdir -p "$DATA_DIR"

redis_up() {
  redis-cli -h 127.0.0.1 -p "$PORT" ping 2>/dev/null | grep -qx PONG
}

wait_up() {
  for _ in {1..50}; do
    if redis_up; then
      return 0
    fi
    sleep 0.1
  done
  echo "[ERROR] staging Redis failed to start" >&2
  return 1
}

wait_down() {
  for _ in {1..50}; do
    if ! redis_up; then
      return 0
    fi
    sleep 0.1
  done
  echo "[ERROR] staging Redis failed to stop" >&2
  return 1
}

stop_redis() {
  if redis_up; then
    redis-cli -h 127.0.0.1 -p "$PORT" shutdown save >/dev/null || true
  fi
}

config_value() {
  redis-cli -h 127.0.0.1 -p "$PORT" --raw CONFIG GET "$1" | tail -n 1
}

trap stop_redis EXIT
if redis_up; then
  echo "[ERROR] :$PORT is already in use; refusing to touch it" >&2
  exit 1
fi

redis-server "$CONFIG"
wait_up

[[ "$(config_value appendonly)" == "yes" ]]
[[ -n "$(config_value save)" ]]
[[ "$(config_value maxmemory-policy)" == "noeviction" ]]
[[ "$(config_value maxmemory)" -gt 0 ]]
[[ "$(config_value latency-monitor-threshold)" -gt 0 ]]

echo "survives-restart" | redis-cli -h 127.0.0.1 -p "$PORT" -x SET "$KEY_PREFIX:job" >/dev/null
redis-cli -h 127.0.0.1 -p "$PORT" ZADD "$KEY_PREFIX:admission" 1 worker-a >/dev/null
redis-cli -h 127.0.0.1 -p "$PORT" BGREWRITEAOF >/dev/null
for _ in {1..100}; do
  if redis-cli -h 127.0.0.1 -p "$PORT" --raw INFO persistence | grep -q '^aof_rewrite_in_progress:0'; then
    break
  fi
  sleep 0.1
done

stop_redis
wait_down
redis-server "$CONFIG"
wait_up

[[ "$(redis-cli -h 127.0.0.1 -p "$PORT" --raw GET "$KEY_PREFIX:job")" == "survives-restart" ]]
[[ "$(redis-cli -h 127.0.0.1 -p "$PORT" --raw ZSCORE "$KEY_PREFIX:admission" worker-a)" == "1" ]]
redis-cli -h 127.0.0.1 -p "$PORT" DEL "$KEY_PREFIX:job" "$KEY_PREFIX:admission" >/dev/null
redis-cli -h 127.0.0.1 -p "$PORT" BGSAVE >/dev/null || true

echo "[OK] staging Redis persistence and restore test passed on :$PORT"
echo "[OK] appendonly=yes, save enabled, maxmemory set, noeviction, latency monitor enabled"
