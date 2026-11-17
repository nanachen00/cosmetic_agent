#!/usr/bin/env bash
# Read-only acceptance checks for the Mac + SSH-forwarded MI300X deployment.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/cosmetic-agent"
ENV_FILE="$BACKEND_ROOT/.env"
BACKEND_PYTHON="${BACKEND_PYTHON:-$BACKEND_ROOT/.venv/bin/python}"

PASS=0
FAIL=0
INCOMPLETE=0
ok() { printf '  [完成] %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  [失敗] %s\n' "$1"; FAIL=$((FAIL + 1)); }
pending() { printf '  [未完成] %s\n' "$1"; INCOMPLETE=$((INCOMPLETE + 1)); }
section() { printf '\n== %s ==\n' "$1"; }

section "設定"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  ok "已找到唯一後端設定檔 cosmetic-agent/.env"
else
  pending "缺少 cosmetic-agent/.env；請從 .env.example 建立，但不要提交秘密"
fi

missing_settings=()
for name in OPENAI_API_KEY DATABASE_URL REDIS_URL NEO4J_URI NEO4J_USER NEO4J_PASSWORD \
  ZEP_API_KEY CHAT_GUEST_SESSION_SECRET CHAT_SECURITY_LOG_HMAC_SECRET; do
  [[ -n "${!name:-}" ]] || missing_settings+=("$name")
done
if (( ${#missing_settings[@]} == 0 )); then
  ok "正式服務必要設定皆有值（未顯示秘密）"
else
  pending "缺少正式設定：${missing_settings[*]}"
fi

section "本機網站與後端"
if curl -fsS --max-time 5 http://127.0.0.1:3000/ >/dev/null 2>&1; then
  ok "Mac 網站 http://127.0.0.1:3000"
else
  pending "Mac 網站未啟動或無法連線"
fi
if curl -fsS --max-time 5 http://127.0.0.1:8010/livez >/dev/null 2>&1; then
  ok "Mac backend /livez"
else
  pending "Mac backend /livez 未就緒"
fi
if curl -fsS --max-time 10 http://127.0.0.1:8010/readyz >/dev/null 2>&1; then
  ok "Mac backend /readyz（依賴整體就緒）"
else
  pending "Mac backend /readyz 未通過；服務未啟動或依賴缺失"
fi

section "SSH 與遠端模型 API"
if [[ -z "${MI300X_SSH_HOST:-}" ]]; then
  pending "尚未設定 MI300X_SSH_HOST=user@實際SSH入口；不以 Jupyter URL 推測"
else
  if "$SCRIPT_DIR/verify-mi300x-ssh.sh"; then
    ok "SSH 目標已證明為 MI300X 且 /mnt/goodagents 為掛載點"
  else
    bad "SSH/MI300X/持久掛載證明失敗"
  fi
fi

taide_root="${TAIDE_BASE_URL:-http://127.0.0.1:8000/v1}"
taide_root="${taide_root%/v1}"
if reply="$(curl -fsS --max-time 90 "$taide_root/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"taide-12b","messages":[{"role":"user","content":"只回答：模型連線正常"}],"max_tokens":32}' \
  | python3 -c 'import json,sys; value=json.load(sys.stdin)["choices"][0]["message"]["content"].strip(); assert value; print(value[:60])' \
  2>/dev/null)"; then
  ok "TAIDE 遠端推論經本機 SSH forward：$reply"
else
  pending "TAIDE 遠端推論未通過（tunnel、服務或權重未就緒）"
fi

embedding_root="${EMBEDDING_BASE_URL:-http://127.0.0.1:8002/v1}"
embedding_root="${embedding_root%/v1}"
if dimension="$(curl -fsS --max-time 30 "$embedding_root/v1/embeddings" \
  -H 'Content-Type: application/json' -d '{"input":"乾燥肌保濕"}' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"][0]["embedding"]))' \
  2>/dev/null)" && [[ "$dimension" == "1024" ]]; then
  ok "BGE-M3 遠端 embedding 維度 1024"
else
  pending "BGE-M3 embedding 未通過（取得維度：${dimension:-無}）"
fi

if [[ "${ENABLE_RERANKER:-0}" == "1" ]]; then
  reranker_root="${RERANKER_BASE_URL:-http://127.0.0.1:8006/v1}"
  if curl -fsS --max-time 10 "${reranker_root%/v1}/readyz" >/dev/null 2>&1; then
    ok "可選 reranker 已就緒"
  else
    bad "ENABLE_RERANKER=1 但 reranker 未就緒"
  fi
else
  pending "reranker 未啟用（正式可選）"
fi

section "GraphRAG"
if [[ ! -x "$BACKEND_PYTHON" ]]; then
  pending "缺少 Mac Python 3.12 venv，無法驗證 Neo4j"
elif [[ -z "${NEO4J_URI:-}" || -z "${NEO4J_USER:-}" || -z "${NEO4J_PASSWORD:-}" ]]; then
  pending "缺少 Neo4j 設定，GraphRAG 未完成"
elif (
  cd "$BACKEND_ROOT" || exit 1
  "$BACKEND_PYTHON" - <<'PY'
from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

repo = KnowledgeGraphRepository.from_env()
try:
    repo.verify_connectivity()
    with repo.session() as session:
        count = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
    assert count > 0, "Neo4j graph is empty"
    print(f"nodes={count}")
finally:
    repo.close()
PY
); then
  ok "Neo4j 圖譜可讀且非空"
else
  bad "Neo4j 圖譜連線或資料檢查失敗"
fi

if graph_result="$(curl -fsS --max-time 90 http://127.0.0.1:8010/api/knowledge/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"玻尿酸","depth":1,"limit":10}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True and d.get("nodes"); print("nodes="+str(len(d["nodes"])))' \
  2>/dev/null)"; then
  ok "GraphRAG 實際 query pipeline：$graph_result"
else
  pending "GraphRAG 實際 query pipeline 未通過（backend、Neo4j、embedding 或設定未就緒）"
fi

section "照片分析"
photo_root="${PHOTO_MODEL_BASE_URL:-http://127.0.0.1:8025}"
if [[ "${ENABLE_PHOTO_MODEL:-0}" != "1" ]]; then
  pending "照片模型未啟用；照片分析仍屬實驗功能"
elif ! curl -fsS --max-time 10 "$photo_root/health" \
  | python3 -c 'import json,sys; assert json.load(sys.stdin).get("ready") is True' \
  >/dev/null 2>&1; then
  bad "照片模型程序存在但模型尚未 ready"
elif photo_result="$(curl -fsS --max-time 180 "$photo_root/analyze" \
  -H 'Content-Type: application/json' -d '{"sample":true,"note":"部署驗證"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d,dict) and not d.get("error"); print("keys="+",".join(sorted(d)[:8]))' \
  2>/dev/null)"; then
  ok "照片分析實際推論：$photo_result"
else
  bad "照片模型 ready，但實際範例分析失敗"
fi

section "重建後持久模型檔"
if [[ -z "${MI300X_SSH_HOST:-}" ]]; then
  pending "無 SSH 目標，無法證明重建後模型仍位於 /mnt/goodagents"
elif [[ -z "${MI300X_CODE_ROOT:-}" ]]; then
  pending "請明確設定 MI300X_CODE_ROOT 為遠端此專案絕對路徑，再執行重建後驗證"
elif [[ ! "$MI300X_CODE_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  bad "MI300X_CODE_ROOT 必須是無空白的安全絕對路徑"
else
  remote_models="taide-prod,embedding"
  [[ "${ENABLE_RERANKER:-0}" == "1" ]] && remote_models+=",reranker"
  [[ "${ENABLE_PHOTO_MODEL:-0}" == "1" ]] && remote_models+=",photo-base"
  remote_command="cd '$MI300X_CODE_ROOT' && python3 ops/runtime/verify-model-artifacts.py --root /mnt/goodagents --models '$remote_models'"
  if ssh -T -p "${MI300X_SSH_PORT:-22}" -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes "$MI300X_SSH_HOST" "$remote_command"; then
    ok "重建後持久模型檔、revision metadata 與 SHA-256 均通過"
  else
    bad "遠端持久模型驗證失敗；不得宣稱權重已恢復"
  fi
fi

printf '\n總結：完成 %d；失敗 %d；未完成 %d\n' "$PASS" "$FAIL" "$INCOMPLETE"
(( FAIL == 0 && INCOMPLETE == 0 ))
