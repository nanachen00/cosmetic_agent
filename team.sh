#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/lib/jenkins/tomod"

FRONTEND_PORT="3000"
STABLE_PORT="8010"
DEV_PORT="8011"
TAIDE_MAIN_PORT="8000"
TAIDE_SKIN_PORT="8004"

port_pid() {
    local port="$1"

    netstat -tlnp 2>/dev/null \
        | grep ":${port} " \
        | grep -oP '\d+(?=/)' \
        | head -1 || true
}

port_status() {
    local port="$1"
    local service="$2"
    local pid

    pid="$(port_pid "$port")"

    if [ -n "$pid" ]; then
        printf "%-22s port %-5s RUNNING  PID %s\n" \
            "$service" "$port" "$pid"
    else
        printf "%-22s port %-5s STOPPED\n" \
            "$service" "$port"
    fi
}

health_http() {
    local name="$1"
    local url="$2"

    printf "%-22s " "$name"

    if curl --fail --silent --max-time 10 "$url" >/dev/null; then
        echo "OK"
    else
        echo "FAILED"
    fi
}

show_status() {
    echo "=== BLINK AI 服務狀態 ==="

    port_status "$FRONTEND_PORT" "Frontend"
    port_status "$STABLE_PORT" "Stable API"
    port_status "$DEV_PORT" "Development API"
    port_status "$TAIDE_MAIN_PORT" "TAIDE Main"
    port_status "$TAIDE_SKIN_PORT" "TAIDE Skin"
}

show_health() {
    echo "=== BLINK AI 健康檢查 ==="

    health_http \
        "Frontend" \
        "http://localhost:${FRONTEND_PORT}"

    health_http \
        "Stable API" \
        "http://localhost:${STABLE_PORT}/api/v1/health"

    health_http \
        "Development API" \
        "http://localhost:${DEV_PORT}/api/v1/health"

    echo
    echo "TAIDE 服務以 Port 狀態判斷："
    port_status "$TAIDE_MAIN_PORT" "TAIDE Main"
    port_status "$TAIDE_SKIN_PORT" "TAIDE Skin"
}

dev_start() {
    echo "正在啟動 Development API（8011）……"
    bash "$ROOT/restart_api_dev.sh"

    echo
    echo "正在執行健康檢查……"

    if curl --fail --silent --max-time 10 \
        "http://localhost:${DEV_PORT}/api/v1/health"; then
        echo
        echo "Development API 已就緒。"
        echo "URL: http://localhost:${DEV_PORT}"
        echo "Log: /tmp/api_8011.log"
    else
        echo
        echo "Development API 健康檢查失敗。"
        echo "請執行："
        echo "tail -f /tmp/api_8011.log"
        exit 1
    fi
}

dev_stop() {
    echo "正在停止 Development API（8011）……"
    bash "$ROOT/stop_api_dev.sh"
    echo "Development API 已停止。"
}

frontend_start() {
    echo "正在啟動 Frontend（3000）……"
    bash "$ROOT/restart_frontend.sh"
}

start_all() {
    echo "容器重啟後才使用此指令。"
    bash "$ROOT/start_all.sh"
}

show_logs() {
    local service="${1:-}"

    case "$service" in
        frontend)
            tail -f /tmp/frontend_3000.log
            ;;
        stable)
            tail -f /tmp/api_8010.log
            ;;
        dev)
            tail -f /tmp/api_8011.log
            ;;
        taide-main)
            tail -f /tmp/taide_8000.log
            ;;
        taide-skin)
            tail -f /tmp/taide_8004.log
            ;;
        *)
            echo "請指定要查看的服務："
            echo "./team.sh logs frontend"
            echo "./team.sh logs stable"
            echo "./team.sh logs dev"
            echo "./team.sh logs taide-main"
            echo "./team.sh logs taide-skin"
            exit 1
            ;;
    esac
}

show_help() {
    cat <<'EOF'
BLINK AI 團隊操作工具

用法：

  ./team.sh status
      查看所有服務與 PID，不修改任何服務。

  ./team.sh health
      執行前端、Stable 與 Dev 健康檢查。

  ./team.sh dev-start
      重啟 Development API（8011）。
      只操作 cosmetic-agent/，不影響 Stable 8010。

  ./team.sh dev-stop
      停止 Development API（8011）。

  ./team.sh frontend-start
      重啟 Next.js Frontend（3000）。

  ./team.sh logs <service>
      查看服務 Log。

      service 可使用：
      frontend、stable、dev、taide-main、taide-skin

  ./team.sh start-all
      容器重新啟動後，啟動完整服務。

一般測試人員建議只使用：

  ./team.sh status
  ./team.sh health
  ./team.sh logs dev

後端開發者可以使用：

  ./team.sh dev-start
  ./team.sh dev-stop

Stable 8010 不由本工具更新或重啟。
EOF
}

COMMAND="${1:-help}"

case "$COMMAND" in
    status)
        show_status
        ;;
    health)
        show_health
        ;;
    dev-start)
        dev_start
        ;;
    dev-stop)
        dev_stop
        ;;
    frontend-start)
        frontend_start
        ;;
    logs)
        show_logs "${2:-}"
        ;;
    start-all)
        start_all
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo "未知指令：$COMMAND"
        echo
        show_help
        exit 1
        ;;
esac