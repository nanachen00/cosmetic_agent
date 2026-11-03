#!/usr/bin/env bash
# System packages: PostgreSQL, Redis, build tools. Data dirs live on PERSIST_ROOT.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

log "10. 系統套件"
mkdir -p "$PERSIST_ROOT" "$MODEL_DIR" "$LOGDIR" "$PIDDIR"
for path in "$PERSIST_ROOT" "$ROOT"; do
  if [[ "$(df --output=target "$path" | tail -1)" == "/" ]]; then
    echo "[WARN] $path 落在容器根目錄 overlay，不是 host 掛載的磁碟。"
    echo "[WARN] 容器重建時資料會再次消失；服務復原後請聯繫管理員用 -v 把 host 目錄掛到 $path。"
  fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  git curl ca-certificates build-essential iproute2 tmux jq \
  postgresql postgresql-contrib redis-server libpq-dev
echo "PostgreSQL: $(ls /usr/lib/postgresql)"
echo "Redis: $(redis-server --version)"
