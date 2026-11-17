#!/usr/bin/env bash
# MI300X model-host utilities only. Databases and backend run on the Mac.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../runtime.conf"
"$SCRIPT_DIR/../verify-mi300x-host.sh"
"$SCRIPT_DIR/../verify-mi300x-mount.sh"

log "10. 系統套件"
mkdir -p "$LOGDIR" "$PIDDIR"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  git curl ca-certificates build-essential iproute2 tmux jq rsync util-linux
