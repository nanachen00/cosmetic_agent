#!/usr/bin/env bash
# 唯讀：找出現在 production（:8010，及 PORTS 指定的其他埠）實際在哪個目錄跑，供填寫 prod_roots.json。不寫任何檔案、不送任何訊號。
#   ./find_prod_roots.sh            # 預設查 8010
#   PORTS="8010 8011" ./find_prod_roots.sh
set -u
for port in ${PORTS:-8010}; do
  pids=$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  [ -z "$pids" ] && { echo ":$port  (沒有 listener，或權限不足看不到 pid；可改用 sudo ss -ltnp)"; continue; }
  for pid in $pids; do
    echo ":$port pid=$pid"
    echo "   cwd     = $(readlink "/proc/$pid/cwd" 2>/dev/null || echo '?')"
    echo "   exe     = $(readlink "/proc/$pid/exe" 2>/dev/null || echo '?')"
    echo "   cmdline = $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-300)"
    unit=$(systemctl status "$pid" 2>/dev/null | head -1); [ -n "$unit" ] && echo "   systemd = $unit"
  done
done
echo "→ 確認後把 cosmetic-agent 的程式根目錄（含 agents/root/orchestrator.py 的那一層）寫進 prod_roots.json 的 prod_roots，並填 confirmed_by/confirmed_on。"
