#!/usr/bin/env bash
# Backend (FastAPI :8010, router :8001) Python environment — no GPU packages.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

log "20. Backend 環境 $BACKEND_ENV"
if [[ ! -x "$BACKEND_ENV/bin/python" ]]; then
  if [[ -x "$CONDA" ]]; then
    "$CONDA" create -y --prefix "$BACKEND_ENV" python=3.11 pip
  else
    python3 -m venv "$BACKEND_ENV"
  fi
fi
PY="$BACKEND_ENV/bin/python"
"$PY" -m pip install -U pip wheel
"$PY" -m pip install -r "$ROOT/cosmetic-agent/requirements-full.txt"
"$PY" -m pip check || echo "[WARN] pip check 有警告，請檢視上方輸出"
"$PY" - <<'PY'
import fastapi, starlette, pydantic, openai, neo4j, psycopg, argon2, redis, zep_cloud
print("fastapi", fastapi.__version__, "| starlette", starlette.__version__, "| pydantic", pydantic.__version__)
print("openai", openai.__version__, "| neo4j", neo4j.__version__, "| psycopg", psycopg.__version__)
assert int(starlette.__version__.split(".")[0]) < 1, "Starlette 1.x breaks app.add_event_handler()"
PY
