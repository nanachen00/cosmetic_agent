#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="/var/lib/jenkins/tomod/cosmetic-agent"
FRONTEND_DIR="/var/lib/jenkins/tomod/frontend"
PYTHON_BIN="/opt/conda/envs/rocm_env/bin/python"
export PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}"

cd "$BACKEND_DIR"

sudo -u postgres psql \
  -d blinkai_dev \
  -v ON_ERROR_STOP=1 \
  -f database/postgres/migrations/0003_conversation_client_id.sql

set -a
source .env
set +a
export AUTH_STORAGE_BACKEND=postgres

"$PYTHON_BIN" -m py_compile \
  api/api_server.py \
  app/api/routes/quiz.py \
  database/profile_repository.py \
  database/postgres/repositories/conversation.py

"$PYTHON_BIN" scripts/check_postgres_conversation.py
"$PYTHON_BIN" scripts/audit_data_boundaries.py
"$PYTHON_BIN" scripts/ai_stack_inventory.py

cd "$FRONTEND_DIR"
npm run typecheck

cd "$BACKEND_DIR"
bash /var/lib/jenkins/tomod/restart_api_dev.sh

curl --fail --silent --show-error \
  http://127.0.0.1:8011/api/v1/health

echo
echo "BLINKAI_DEV_VALIDATION_OK"
