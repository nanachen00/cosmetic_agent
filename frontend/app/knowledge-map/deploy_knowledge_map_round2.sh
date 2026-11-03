#!/usr/bin/env bash
set -euo pipefail

ROOT=/var/lib/jenkins/tomod
STAGE="$ROOT/frontend/app/knowledge-map"
BACKUP="$ROOT/recovery-backups/knowledge-map-round2-20261102"
LOG="$STAGE/knowledge-map-round2-deploy.log"

mkdir -p "$BACKUP"
cp "$ROOT/cosmetic-agent/app/services/knowledge_query_pipeline.py" "$BACKUP/knowledge_query_pipeline.py"
cp "$ROOT/cosmetic-agent/app/api/routes/knowledge_explorer.py" "$BACKUP/knowledge_explorer.py"
cp "$ROOT/frontend/app/knowledge-map/page.tsx" "$BACKUP/page.tsx"
cp "$ROOT/frontend/app/knowledge-map/KnowledgeMap.module.css" "$BACKUP/KnowledgeMap.module.css"
cp "$ROOT/frontend/tests/source-contracts.mjs" "$BACKUP/source-contracts.mjs"

python3 "$STAGE/patch_knowledge_map_common_gaps.py"
python3 "$STAGE/apply_knowledge_query_outcomes.py" \
  "$ROOT/cosmetic-agent/app/services/knowledge_query_pipeline.py" \
  "$ROOT/cosmetic-agent/app/api/routes/knowledge_explorer.py"

cp "$STAGE/remote-knowledge-map-current.tsx" "$ROOT/frontend/app/knowledge-map/page.tsx"
cp "$STAGE/remote-knowledge-map-current.css" "$ROOT/frontend/app/knowledge-map/KnowledgeMap.module.css"
cp "$STAGE/remote-source-contracts-current.mjs" "$ROOT/frontend/tests/source-contracts.mjs"
cp "$STAGE/test_knowledge_query_interpreter.py" "$ROOT/cosmetic-agent/tests/test_knowledge_query_interpreter.py"

PYTHONPYCACHEPREFIX=/tmp/blink-km-pycache python3 -m py_compile \
  "$ROOT/cosmetic-agent/app/services/knowledge_query_pipeline.py" \
  "$ROOT/cosmetic-agent/app/api/routes/knowledge_explorer.py"

cd "$ROOT/cosmetic-agent"
pytest -q tests/test_knowledge_query_interpreter.py tests/test_knowledge_query_pipeline.py

cd "$ROOT/frontend"
node tests/source-contracts.mjs
npx tsc --noEmit
npm run build

printf 'ROUND2_DEPLOY_OK\n' | tee "$STAGE/ROUND2_DEPLOY_OK"
