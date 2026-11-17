#!/usr/bin/env bash
# Detached-service entry point; credentials are loaded from the canonical file.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
service_name="${1:?service name required}"
MAC_RUNTIME_DIR="${2:?runtime directory required}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
set -a
source "$PROJECT_ROOT/cosmetic-agent/.env"
set +a
export LOCAL_BACKEND_OVERRIDE=1 BACKEND_API_URL=http://127.0.0.1:8010
export MEMBER_AUTH_API_BASE_URL="$BACKEND_API_URL"
export TAIDE_BASE_URL="${TAIDE_BASE_URL:-http://127.0.0.1:8000/v1}"
export EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-http://127.0.0.1:8002/v1}"
export PHOTO_MODEL_BASE_URL="${PHOTO_MODEL_BASE_URL:-http://127.0.0.1:8025}"
if [[ -n "${3:-}" ]]; then export PROMETHEUS_MULTIPROC_DIR="$3"; fi
PY="${BACKEND_PYTHON:-$PROJECT_ROOT/cosmetic-agent/.venv/bin/python}"
printf '%s\n' "$$" > "$MAC_RUNTIME_DIR/pids/$service_name.pid"
case "$service_name" in
  router)
    [[ "${ENABLE_TAIDE:-0}" == "1" ]] || { echo '[ERROR] Legacy TAIDE router is disabled' >&2; exit 2; }
    cd "$PROJECT_ROOT"
    export TAIDE_CHAT_URL="${TAIDE_BASE_URL%/v1}" TAIDE_EMBED_URL="${EMBEDDING_BASE_URL%/v1}"
    exec "$PY" "$PROJECT_ROOT/taide_router_8001.py"
    ;;
  backend)
    cd "$PROJECT_ROOT/cosmetic-agent"
    export PYTHONUNBUFFERED=1
    exec "$PY" -m uvicorn api.api_server:app --host 127.0.0.1 --port 8010 --workers "${BACKEND_WORKERS:-1}"
    ;;
  frontend)
    cd "$PROJECT_ROOT/frontend"
    export NEXT_DIST_DIR="${NEXT_DIST_DIR:-.next}"
    [[ "$NEXT_DIST_DIR" =~ ^\.next(-[A-Za-z0-9][A-Za-z0-9_-]*)?$ ]] \
      || { echo '[ERROR] NEXT_DIST_DIR must be .next or a single .next-safe-name' >&2; exit 2; }
    [[ ! -L "$NEXT_DIST_DIR" ]] \
      || { echo '[ERROR] NEXT_DIST_DIR must not be a symlink' >&2; exit 2; }
    exec node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3000
    ;;
  *) echo '[ERROR] Unknown Mac service' >&2; exit 2 ;;
esac
