#!/usr/bin/env bash
# Rebuild the Mac application with locked dependencies. No model download.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || { echo '[ERROR] Mac only'; exit 2; }
for binary in uv npm redis-server psql; do
  command -v "$binary" >/dev/null || { echo "[ERROR] install missing prerequisite: $binary"; exit 2; }
done
cd "$PROJECT_ROOT/cosmetic-agent"
if [[ ! -x .venv/bin/python ]]; then uv venv --python 3.12 .venv; fi
uv pip install --python .venv/bin/python --require-hashes -r requirements-full.lock
uv pip check --python .venv/bin/python
cd "$PROJECT_ROOT/frontend"
npm ci
"$SCRIPT_DIR/build-mac-frontend.sh"
"$SCRIPT_DIR/start-mac-redis.sh"
if [[ -f "$PROJECT_ROOT/cosmetic-agent/.env" ]]; then
  "$PROJECT_ROOT/cosmetic-agent/.venv/bin/python" "$SCRIPT_DIR/init-mac-database.py"
else
  echo '[未完成] Fill cosmetic-agent/.env from .env.example before database initialization and startup'
fi
