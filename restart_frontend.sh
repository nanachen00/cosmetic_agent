#!/usr/bin/env bash
set -euo pipefail

FRONTEND_DIR="/var/lib/jenkins/tomod/frontend"
FRONTEND_PORT="3000"
LOG_FILE="/tmp/frontend_3000.log"
PID_FILE="/tmp/frontend_3000.pid"
NODE_HOME="/opt/node-v22.23.4"

export PATH="$NODE_HOME/bin:/usr/local/bin:/usr/bin:/bin"
export LOCAL_BACKEND_OVERRIDE="1"
export BACKEND_API_URL="http://127.0.0.1:8010"
export INTERNAL_API_ORIGIN="http://127.0.0.1:8010"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "找不到 Node/npm；預期位置：$NODE_HOME/bin"
  exit 1
fi

cd "$FRONTEND_DIR"

if [[ ! -d node_modules ]]; then
  echo "找不到 node_modules，依 package-lock 安裝套件……"
  npm ci --no-audit --no-fund
fi

echo "檢查前端型別……"
npm run typecheck

port_pids() {
  ss -H -ltnp "sport = :${FRONTEND_PORT}" 2>/dev/null \
    | grep -oP 'pid=\K\d+' \
    | sort -u || true
}

stop_pid_tree() {
  local pid="$1"
  local process_group current_group
  process_group="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  current_group="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ' || true)"

  if [[ -n "$process_group" && "$process_group" != "$current_group" ]]; then
    kill -TERM -- "-$process_group" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

OLD_PIDS="$(port_pids)"
if [[ -n "$OLD_PIDS" ]]; then
  echo "停止舊 Frontend PID：$(echo "$OLD_PIDS" | tr '\n' ' ')"
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && stop_pid_tree "$pid"
  done <<<"$OLD_PIDS"

  for attempt in $(seq 1 20); do
    [[ -z "$(port_pids)" ]] && break
    sleep 0.25
  done

  REMAINING_PIDS="$(port_pids)"
  if [[ -n "$REMAINING_PIDS" ]]; then
    echo "舊程序未正常退出，強制停止：$(echo "$REMAINING_PIDS" | tr '\n' ' ')"
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
    done <<<"$REMAINING_PIDS"
  fi
fi

rm -f "$PID_FILE"

echo "建立正式版前端……"
npm run build

echo "以 production 模式啟動 Frontend……"
nohup setsid "$NODE_HOME/bin/node" node_modules/next/dist/bin/next \
  start --hostname 0.0.0.0 --port "$FRONTEND_PORT" \
  >"$LOG_FILE" 2>&1 &

NEW_PID="$!"
echo "$NEW_PID" >"$PID_FILE"
echo "Frontend PID：$NEW_PID"

for attempt in $(seq 1 30); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    echo "Frontend 程序提前結束。"
    tail -n 60 "$LOG_FILE" || true
    exit 1
  fi
  if curl --fail --silent "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null; then
    echo "Frontend 啟動成功：http://127.0.0.1:${FRONTEND_PORT}"
    echo "Log：$LOG_FILE"
    exit 0
  fi
  sleep 1
done

echo "Frontend 啟動失敗或逾時。"
tail -n 60 "$LOG_FILE" || true
exit 1
