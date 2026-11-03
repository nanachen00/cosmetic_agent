#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGING_RUNTIME_ROOT="$(readlink -m "${STAGING_RUNTIME_ROOT:-$PROJECT_ROOT/.runtime/staging}")"
METRICS_DIR="$(readlink -m "${PROMETHEUS_MULTIPROC_DIR:-$STAGING_RUNTIME_ROOT/prometheus}")"

case "$METRICS_DIR" in
  "$STAGING_RUNTIME_ROOT"/*) ;;
  *)
    echo "PROMETHEUS_MULTIPROC_DIR must stay under staging runtime" >&2
    exit 1
    ;;
esac

if [[ "${APP_ENV:-}" != "staging" ]]; then
  echo "APP_ENV must be staging" >&2
  exit 1
fi
if [[ "${STAGING_CONFIRM_ISOLATED_DATA:-}" != "1" ]]; then
  echo "STAGING_CONFIRM_ISOLATED_DATA=1 is required" >&2
  exit 1
fi

workers="${STAGING_WORKERS:-1}"
case "$workers" in
  1|2|4) ;;
  *)
    echo "STAGING_WORKERS must be 1, 2 or 4" >&2
    exit 1
    ;;
esac

for required in DATABASE_URL REDIS_URL ZEP_API_KEY NEO4J_URI NEO4J_USER   NEO4J_PASSWORD CHAT_GUEST_SESSION_SECRET METRICS_BEARER_TOKEN APP_RELEASE; do
  if [[ -z "${!required:-}" ]]; then
    echo "$required is required for staging" >&2
    exit 1
  fi
done

if [[ -f "$PROJECT_ROOT/scripts/verify_staging_isolation.py" ]]; then
  "${PYTHON_BIN:-python3}" "$PROJECT_ROOT/scripts/verify_staging_isolation.py"
fi

mkdir -p "$METRICS_DIR"
chmod 700 "$STAGING_RUNTIME_ROOT" "$METRICS_DIR"
export PROMETHEUS_MULTIPROC_DIR="$METRICS_DIR"

cd "$PROJECT_ROOT"
exec "${PYTHON_BIN:-python3}" -m uvicorn api.api_server:app   --host 127.0.0.1 --port 18110 --workers "$workers"
