#!/usr/bin/env bash
# Local PostgreSQL + Redis on PERSIST_ROOT, reconstructed schema, observation catalog.
# Set EXTERNAL_DATABASE_URL to use a managed PostgreSQL instead of the local cluster.
set -Eeuo pipefail
source "$(dirname "$0")/../runtime.conf"

ENV_FILE="$ROOT/cosmetic-agent/.env"
SCHEMA="$ROOT/cosmetic-agent/database/postgres/migrations/0001_reconstructed_schema.sql"

if [[ -n "${EXTERNAL_DATABASE_URL:-}" ]]; then
  DATABASE_URL="$EXTERNAL_DATABASE_URL"
else
  log "60. 本機 PostgreSQL（資料目錄 $PGDATA）"
  PG_BIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
  mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    su postgres -c "$PG_BIN/initdb -D '$PGDATA' --encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256"
    echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
    echo "port = $PG_PORT" >> "$PGDATA/postgresql.conf"
  fi
  su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -l '$PGDATA/server.log' status" >/dev/null 2>&1 \
    || su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -l '$PGDATA/server.log' -w start"

  PW_FILE="$PERSIST_ROOT/.pg_password"
  if [[ ! -f "$PW_FILE" ]]; then
    umask 077; python3 -c 'import secrets;print(secrets.token_hex(24))' > "$PW_FILE"
  fi
  PG_PASSWORD="$(cat "$PW_FILE")"
  su postgres -c "psql -p $PG_PORT -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'\"" | grep -q 1 \
    || su postgres -c "psql -p $PG_PORT -c \"CREATE ROLE $PG_USER LOGIN PASSWORD '$PG_PASSWORD'\""
  su postgres -c "psql -p $PG_PORT -tAc \"SELECT 1 FROM pg_database WHERE datname='$PG_DB'\"" | grep -q 1 \
    || su postgres -c "createdb -p $PG_PORT -O $PG_USER $PG_DB"
  su postgres -c "psql -p $PG_PORT -d $PG_DB -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto'"
  DATABASE_URL="postgresql://$PG_USER:$PG_PASSWORD@127.0.0.1:$PG_PORT/$PG_DB"
fi

log "套用重建的資料表結構"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$SCHEMA"
psql "$DATABASE_URL" -tAc "SELECT count(*) || ' tables' FROM information_schema.tables WHERE table_schema='public'"

log "Redis（AOF 持久化，資料目錄 $REDIS_DIR，port $REDIS_PORT）"
mkdir -p "$REDIS_DIR"
if ! redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --dir "$REDIS_DIR" \
    --appendonly yes --daemonize yes --logfile "$REDIS_DIR/redis.log"
fi
redis-cli -p "$REDIS_PORT" ping

# Write connection strings into .env (other secrets are filled by the team).
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/cosmetic-agent/.env.example" "$ENV_FILE"; chmod 600 "$ENV_FILE"
fi
python3 - "$ENV_FILE" "$DATABASE_URL" "redis://127.0.0.1:$REDIS_PORT/0" <<'PY'
import sys, re, pathlib
path, db, redis_url = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
for key, value in (("DATABASE_URL", db), ("REDIS_URL", redis_url)):
    line = f"{key}={value}"
    text = re.sub(rf"^{key}=.*$", line, text, flags=re.M) if re.search(rf"^{key}=", text, re.M) else text + f"\n{line}\n"
path.write_text(text)
print("updated DATABASE_URL / REDIS_URL in", path)
PY

log "從 Neo4j 重建膚況標籤目錄（需要 .env 內的 NEO4J_*）"
CATALOG="$ROOT/cosmetic-agent/agents/skin_tracking/skin_observation_labels_100.json"
if [[ -f "$CATALOG" ]]; then
  echo "[SKIP] 已存在 $CATALOG"
else
  ( set -a; source "$ENV_FILE"; set +a
    cd "$ROOT/cosmetic-agent" && "$BACKEND_ENV/bin/python" scripts/rebuild_observation_catalog_from_neo4j.py ) \
    || echo "[WARN] 標籤目錄重建失敗：膚況紀錄功能會停用，其他功能不受影響。請先填好 NEO4J_* 再重跑本腳本。"
fi
