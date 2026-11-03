#!/usr/bin/env bash
# Post-restore acceptance: offline unit tests, service health, real generation through every hop.
set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/runtime.conf"
ENV_FILE="$ROOT/cosmetic-agent/.env"
PASS=0; FAILS=()
ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAILS+=("$1"); }

log "1. 重建模組的離線單元測試（不連外、不呼叫模型）"
(
  cd "$ROOT/cosmetic-agent"
  "$BACKEND_ENV/bin/python" -m pytest -q -p no:cacheprovider \
    test/test_skin_extraction_provider.py test/test_skin_zone_extraction.py \
    tests/regression/test_observation_tag_terra_severity.py test/test_response_composer.py \
    test/test_skin_tracking_service.py test/test_skin_tracking_stability.py \
    tests/test_skin_tracking_first_message_semantics.py tests/test_skin_tracking_location_flow.py \
    test/test_oauth_onboarding.py tests/test_oauth_routes.py
) && ok "unit tests" || bad "unit tests（見上方 pytest 輸出）"

log "2. 後端可完整載入（所有路由註冊）"
(
  cd "$ROOT/cosmetic-agent"; set -a; source "$ENV_FILE"; set +a
  "$BACKEND_ENV/bin/python" - <<'PY'
from api.api_server import app
paths = {route.path for route in app.routes}
required = {"/api/auth/session", "/api/auth/password/login", "/api/auth/password/register/request",
            "/api/auth/password/register/confirm", "/api/auth/google/login", "/api/auth/line/login",
            "/readyz"}
missing = sorted(required - paths)
print(f"{len(paths)} routes registered")
assert not missing, f"missing routes: {missing}"
PY
) && ok "api_server import + routes" || bad "api_server import"

log "3. 服務健康"
"$ROOT/ops/runtime/status-production.sh" && ok "all services UP" || bad "some services DOWN"

log "4. 實際生成：vLLM 直連 → 閘道 8000 → router 8001 → 8003"
for url in "http://127.0.0.1:$VLLM_PROD_PORT" "http://127.0.0.1:8000" "http://127.0.0.1:8001" "http://127.0.0.1:8003"; do
  reply="$(curl -sS --max-time 60 "$url/v1/chat/completions" -H 'Content-Type: application/json' \
    -d '{"model":"taide-12b","messages":[{"role":"user","content":"用一句繁體中文介紹玻尿酸。"}],"max_tokens":80}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip()[:80])' 2>/dev/null)"
  [[ -n "$reply" ]] && ok "$url → $reply" || bad "generation via $url"
done
if [[ "$ENABLE_AB_MODEL" == "1" ]]; then
  reply="$(curl -sS --max-time 60 http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"taide-12b-2602","messages":[{"role":"user","content":"用一句繁體中文介紹玻尿酸。"}],"max_tokens":80}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip()[:80])' 2>/dev/null)"
  [[ -n "$reply" ]] && ok "A/B taide-12b-2602 → $reply" || bad "A/B generation"
fi

log "5. Embedding 維度"
dim="$(curl -sS --max-time 20 http://127.0.0.1:8002/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"input":"乾燥肌保濕"}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"][0]["embedding"]))' 2>/dev/null)"
[[ "$dim" == "1024" ]] && ok "BGE-M3 embedding dim=1024（與 Neo4j 既有向量相容）" || bad "embedding dim=$dim"

log "6. 資料庫與 Neo4j"
(
  cd "$ROOT/cosmetic-agent"; set -a; source "$ENV_FILE"; set +a
  "$BACKEND_ENV/bin/python" - <<'PY'
from database.postgres.connection import check_postgres_connection
print("postgres:", check_postgres_connection())
from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository
repo = KnowledgeGraphRepository.from_env()
with repo.session() as s:
    print("neo4j nodes:", s.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    print("SkinObservationTag:", s.run("MATCH (t:SkinObservationTag) RETURN count(t) AS c").single()["c"])
repo.close()
PY
) && ok "PostgreSQL + Neo4j Aura" || bad "PostgreSQL / Neo4j 連線"

echo
echo "通過 $PASS 項；失敗 ${#FAILS[@]} 項"
for item in "${FAILS[@]}"; do echo "  - $item"; done
[[ ${#FAILS[@]} -eq 0 ]]
