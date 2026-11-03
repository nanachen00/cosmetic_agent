#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${ROOT:-/var/lib/jenkins/tomod}"
OUT="${OUT:-$ROOT/_trace_export}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
NAME="blink-e2e-trace-inputs-$STAMP"
STAGE="$OUT/$NAME"
ARCHIVE="$OUT/$NAME.tar.gz"

mkdir -p "$STAGE"

log() { printf '%s\n' "$*"; }

copy_rel() {
  local rel="$1"
  local src="$ROOT/$rel"
  local dst="$STAGE/$rel"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -p -- "$src" "$dst"
    printf 'COPIED\t%s\n' "$rel" >> "$STAGE/manifest.tsv"
  else
    printf 'MISSING\t%s\n' "$rel" >> "$STAGE/manifest.tsv"
  fi
}

cat > "$STAGE/README.txt" <<'EOF'
BLINK E2E latency-trace input snapshot.

Purpose:
- inspect the current :8010 chat request path;
- add request/stage timing without changing business behavior;
- correlate browser multi-VU observations with backend request IDs;
- identify queue / Agent / DB / retrieval / LLM latency.

This is an analysis snapshot, not a deployable package.

Excluded intentionally:
- .env files
- model weights
- database dumps
- browser auth state
- application logs
- .git objects
- backups / *.bak / checkpoints
EOF

: > "$STAGE/manifest.tsv"

FILES=(
  "cosmetic-agent/api/api_server.py"
  "cosmetic-agent/agents/root/orchestrator.py"
  "cosmetic-agent/agents/root/orchestrator2.py"
  "cosmetic-agent/agents/knowledge/knowledge_agent.py"
  "cosmetic-agent/agents/knowledge/knowledge_sub_agent.py"
  "cosmetic-agent/agents/product/product_agent.py"
  "cosmetic-agent/tools/agent/intent_parser.py"
  "cosmetic-agent/tools/agent/input_guard.py"
  "cosmetic-agent/tools/a2a/productknowledge_orchestrator.py"
  "cosmetic-agent/tools/a2a/product_knowledge_gate.py"
  "cosmetic-agent/tools/a2a/product_user_bridge.py"
  "cosmetic-agent/tools/a2a/skin_product_bridge.py"
  "cosmetic-agent/tools/search/graph_search.py"
  "cosmetic-agent/tools/search/ingredient_tool.py"
  "cosmetic-agent/tools/search/ingredient_extractor.py"
  "cosmetic-agent/tools/search/knowledge_intent.py"
  "cosmetic-agent/tools/search/risk_rule_tool.py"
  "cosmetic-agent/tools/search/skinconcern_tool.py"
  "cosmetic-agent/tools/getzep/conversation_memory_service.py"
  "cosmetic-agent/tools/getzep/zep_memory_adapter.py"
  "cosmetic-agent/tools/getzep/zep_connection.py"
  "cosmetic-agent/database/db_connect.py"
  "cosmetic-agent/database/cosmetic_db_router.py"
  "cosmetic-agent/database/profile_repository.py"
  "cosmetic-agent/models/taide_client.py"
  "cosmetic-agent/schemas/intent.py"
  "cosmetic-agent/app/security/chat_guard.py"
  "cosmetic-agent/core/admission.py"
  "cosmetic-agent/core/observability.py"
  "cosmetic-agent/app/api/routes/health.py"
  "cosmetic-agent/requirements-backend.txt"
  "frontend/lib/api.ts"
  "frontend/lib/api-origin.ts"
  "frontend/lib/chat-flow.ts"
  "frontend/app/chat/page.tsx"
  "frontend/middleware.ts"
  "taide_router_8001.py"
  "api_server_embedding.py"
  "taide_server_graphrag.py"
  "ops/vllm-v092/vllm_v092_start_8200_r5_textonly.sh"
  "ops/vllm-v092/vllm_v092_stop.sh"
)

for rel in "${FILES[@]}"; do
  copy_rel "$rel"
done

if [[ -d "$ROOT/cosmetic-agent/.git" ]]; then
  {
    echo "===== HEAD ====="
    git -C "$ROOT/cosmetic-agent" rev-parse HEAD || true
    echo
    echo "===== BRANCH ====="
    git -C "$ROOT/cosmetic-agent" branch --show-current || true
    echo
    echo "===== STATUS ====="
    git -C "$ROOT/cosmetic-agent" status --short || true
    echo
    echo "===== RECENT COMMITS ====="
    git -C "$ROOT/cosmetic-agent" log --date=iso --format='%h %ad %s' -20 || true
  } > "$STAGE/cosmetic-agent-git.txt"
fi

if [[ -d "$ROOT/frontend/.git" ]]; then
  {
    echo "===== HEAD ====="
    git -C "$ROOT/frontend" rev-parse HEAD || true
    echo
    echo "===== BRANCH ====="
    git -C "$ROOT/frontend" branch --show-current || true
    echo
    echo "===== STATUS ====="
    git -C "$ROOT/frontend" status --short || true
  } > "$STAGE/frontend-git.txt"
fi

PORTS=(8000 8001 8002 8003 8010 8011 8200)

{
  echo "===== LISTENERS ====="
  ss -ltnp 2>/dev/null | grep -E ':(8000|8001|8002|8003|8010|8011|8200)\b' || true
  echo
} > "$STAGE/runtime-services.txt"

for PORT in "${PORTS[@]}"; do
  PID="$(
    ss -H -ltnp 2>/dev/null \
      | grep -E ":${PORT}([[:space:]]|$)" \
      | grep -oP 'pid=\K[0-9]+' \
      | head -1 \
      || true
  )"

  {
    echo "===== PORT $PORT ====="
    echo "pid=${PID:-NONE}"

    if [[ -n "$PID" && -d "/proc/$PID" ]]; then
      printf 'exe='
      readlink -f "/proc/$PID/exe" || true
      printf 'cwd='
      readlink -f "/proc/$PID/cwd" || true
      printf 'cmd='
      tr '\0' ' ' < "/proc/$PID/cmdline" || true
      echo

      echo
      echo "===== ENV NAMES ====="
      tr '\0' '\n' < "/proc/$PID/environ" \
        | sed -n 's/^\([^=]*\)=.*/\1/p' \
        | sort -u || true

      echo
      echo "===== SAFE ENV VALUES ====="
      python3 - "$PID" <<'PY'
from pathlib import Path
import sys

pid = sys.argv[1]
items = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
env = {}
for item in items:
    if b"=" not in item:
        continue
    k, v = item.split(b"=", 1)
    env[k.decode(errors="replace")] = v.decode(errors="replace")

allow_values = {
    "TAIDE_BASE_URL",
    "OPENAI_BASE_URL",
    "LITELLM_BASE_URL",
    "KNOWLEDGE_RESPONSE_PROVIDER",
    "KNOWLEDGE_RESPONSE_MODEL",
    "PRODUCT_INGREDIENT_EXTRACTOR",
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "EMBEDDING_PORT",
    "EMBEDDING_DEVICE",
    "EMBEDDING_MAX_CONCURRENT",
    "EMBEDDING_QUEUE_TIMEOUT_SECONDS",
    "CHAT_MAX_RUNNING",
    "CHAT_MAX_QUEUED",
    "CHAT_QUEUE_TIMEOUT_SECONDS",
    "CHAT_IDEMPOTENCY_TTL_SECONDS",
    "CHAT_MAX_INPUT_TOKENS",
    "CHAT_MAX_OUTPUT_TOKENS",
    "CHAT_TOKEN_BUDGET",
    "VLLM_USE_V1",
}

for key in sorted(allow_values):
    if key in env:
        print(f"{key}={env[key]}")

for key in sorted(env):
    upper = key.upper()
    if any(word in upper for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")):
        print(f"{key}=<SET_REDACTED>")
PY
    fi
    echo
  } >> "$STAGE/runtime-services.txt"
done

if command -v nginx >/dev/null 2>&1; then
  nginx -T 2>/dev/null \
    | grep -nE 'server_name|location|proxy_pass|proxy_(connect|read|send)_timeout|keepalive' \
    > "$STAGE/nginx-routing.txt" || true
fi

if command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time 10 \
    http://127.0.0.1:8010/openapi.json \
    > "$STAGE/openapi-8010.json" 2> "$STAGE/openapi-8010.error.txt" || true

  if [[ ! -s "$STAGE/openapi-8010.error.txt" ]]; then
    rm -f "$STAGE/openapi-8010.error.txt"
  fi
fi

LITELLM_CONFIG="$ROOT/cosmetic-agent/tools/tests/config.yaml"
if [[ -f "$LITELLM_CONFIG" ]]; then
  mkdir -p "$STAGE/cosmetic-agent/tools/tests"
  python3 - "$LITELLM_CONFIG" "$STAGE/cosmetic-agent/tools/tests/config.yaml" <<'PY'
from pathlib import Path
import re
import sys

src = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
src = re.sub(r'(?im)^(\s*api_key\s*:\s*).*$', r'\1<REDACTED>', src)
Path(sys.argv[2]).write_text(src, encoding="utf-8")
PY
  printf 'COPIED_SANITIZED\t%s\n' \
    "cosmetic-agent/tools/tests/config.yaml" >> "$STAGE/manifest.tsv"
fi

# Sanitize obvious secret values in the staged analysis copy only.
# Original files are never modified.
python3 - "$STAGE" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
text_exts = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml", ".yml", ".sh", ".txt", ".md", ".conf"}

for p in root.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in text_exts:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue

    text = re.sub(r'mongodb(?:\+srv)?://[^\s\'\"]+', '<REDACTED_MONGODB_URI>', text, flags=re.I)
    text = re.sub(r'neo4j(?:\+s|\+ssc|s)?://[^\s\'\"]+', '<REDACTED_NEO4J_URI>', text, flags=re.I)
    text = re.sub(r'\bsk-[A-Za-z0-9_\-]{12,}\b', '<REDACTED_API_KEY>', text)

    p.write_text(text, encoding="utf-8")
PY

grep -RIlE \
  'API[_-]?KEY|PASSWORD|SECRET|TOKEN|mongodb(\+srv)?://|neo4j(\+s|\+ssc|s)?://' \
  "$STAGE" \
  --exclude='potential-secret-files.txt' \
  > "$STAGE/potential-secret-files.txt" 2>/dev/null || true

(
  cd "$STAGE"
  find . -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    > SHA256SUMS.txt
)

tar -czf "$ARCHIVE" -C "$OUT" "$NAME"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
fi

log "===== COMPLETE ====="
log "Archive: $ARCHIVE"
log "Manifest: $STAGE/manifest.tsv"
log "Potential-secret file list: $STAGE/potential-secret-files.txt"
log
log "Original source/runtime files were NOT modified or deleted."
log "The staged analysis copy was sanitized for obvious URI/API-key patterns."
