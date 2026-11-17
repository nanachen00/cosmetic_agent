#!/usr/bin/env bash
# Reproducible offline gates; never source the production .env or call models.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PY="$PROJECT_ROOT/cosmetic-agent/.venv/bin/python"
export OPENAI_API_KEY=offline-test-only
export PYTHON_DOTENV_DISABLED=1
export OPENAI_BASE_URL=http://127.0.0.1:9/v1
export NEO4J_URI=bolt://127.0.0.1:9 NEO4J_USER=offline NEO4J_PASSWORD=offline
export DATABASE_URL=postgresql://offline:offline@127.0.0.1:9/offline
export ZEP_API_KEY=offline-test-only REDIS_URL=""
unset CHAT_JOB_REDIS_URL CHAT_ADMISSION_REDIS_URL
export CHAT_LIMIT_REQUIRE_REDIS=0 API_LIMIT_REQUIRE_REDIS=0 CHAT_ADMISSION_REQUIRE_REDIS=0
export CHAT_JOB_STORE_REQUIRE_REDIS=0 KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS=0
export CHAT_STATE_REQUIRE_REDIS=0
export RUN_LIVE_MODEL_TESTS=0 RUN_LIVE_ZEP_TESTS=0 RUN_LIVE_NEO4J_TESTS=0
export BLINK_OFFLINE_TESTS=1
cd "$PROJECT_ROOT/cosmetic-agent"
"$PY" -m pytest -q
cd "$PROJECT_ROOT/experiments/skin-vlm"
"$PY" -m unittest -q test_contracts
cd "$PROJECT_ROOT/frontend"
npm run test:contracts
npm run typecheck
"$SCRIPT_DIR/build-mac-frontend.sh"
