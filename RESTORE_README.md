# BLINK AI 平台復原包（2026-10-21）

以 2026-10-13 的 source bundle 為基礎，補齊遺失的模組、資料表結構與執行環境，
並把 TAIDE 從 HF Transformers 改為 vLLM 服務。

## 1. 執行順序（在 MI300X 容器內，請用 tmux）

```bash
# 1) 解壓到 /var/lib/jenkins/tomod（若尚未掛載 host 目錄，腳本只會警告不會停止；
#    服務復原後請管理員掛載 /var/lib/jenkins/tomod 與 /data/blink，避免下次重建又消失）
tmux new -s restore
export HF_TOKEN=hf_xxx            # 唯讀 token，帳號需已同意 TAIDE 授權；只放在環境變數
bash /var/lib/jenkins/tomod/ops/runtime/install_all.sh 2>&1 | tee /var/lib/jenkins/tomod/restore.log
# 2) 填寫 cosmetic-agent/.env 的 [REQUIRED] 項目，再重跑標籤目錄重建
bash /var/lib/jenkins/tomod/ops/runtime/install/60_init_database.sh
# 3) 啟動與驗收
bash /var/lib/jenkins/tomod/ops/runtime/start-production.sh
bash /var/lib/jenkins/tomod/ops/runtime/verify_platform.sh
# A/B：同時啟動 TAIDE 12B-2602
ENABLE_AB_MODEL=1 bash /var/lib/jenkins/tomod/ops/runtime/start-production.sh
```

推論引擎直接使用已編譯好的 `/opt/venv-llm`（vLLM v0.8.5 + SGLang v0.4.6.post5），安裝流程不會整個重新編譯。
`install/38_fix_vllm_rocm_c.sh` 會修補 v0.8.5 在 ROCm 上的已知 bug（`_C.abi3.so: undefined symbol: cutlass_mla_decode`），
只重編 `torch_bindings.cpp` 並重新連結；`_C` 正常時會直接跳過。
兩個 TAIDE 權重約 50 GB，下載時間視頻寬而定。

改用 SGLang 服務模型（閘道與後端不需改）：`ENGINE=sglang bash .../start-production.sh`

## 2. 新架構

| Port | 服務 | 變更 |
|---|---|---|
| 8200 | vLLM（或 SGLang）：taide/Gemma-3-TAIDE-12b-Chat（served name `taide-12b`） | **新**，取代 HF Transformers |
| 8201 | 同上：taide/Gemma-3-TAIDE-12b-Chat-2602（`taide-12b-2602`，A/B，預設不啟動） | **新** |
| 8000 | `ops/runtime/taide_vllm_gateway.py` 相容閘道 | 介面與舊 HF 版完全相同（/healthz、600 token 上限、預設 temperature 0.1） |
| 8003 | 同一個閘道 | 取代遺失設定的 LiteLLM |
| 8002 | BGE-M3 embedding（sentence-transformers，未改） | 向量與 Neo4j 既有資料相容 |
| 8001 | TAIDE router（未改） | |
| 8010 | FastAPI backend | |
| 5432 / 6389 | PostgreSQL / Redis，資料目錄在 `/data/blink` | **新**，含持久化 |

vLLM 啟動參數沿用團隊先前在 8200 驗證過的設定（`ops/vllm-v092/`）：`--generation-config auto`、只開文字輸入、預設 `--enforce-eager`。
腳本會依 vLLM 版本自動選擇 `--limit-mm-per-prompt` 的寫法（0.8.x 為 `image=0`）。效能模式：`VLLM_PROFILE=perf`。
若之後改用其他推論環境：`VLLM_ENV=<環境路徑>`。換成 Llama-3-Taiwan-70B：設定 `PROD_MODEL_REPO`、`PROD_MODEL_GATED=0`、`VLLM_PROD_MEM=0.90`、
`VLLM_MAX_MODEL_LEN=8192`、`VLLM_EXTRA_ARGS="--quantization fp8"`、`EMBEDDING_DEVICE=cpu`。

## 3. 重建的檔案（皆在檔頭標示 RECONSTRUCTED）

| 檔案 | 依據 |
|---|---|
| `tools/auth/password_login.py` | api_server、oauth routes、postgres_password_login 的呼叫點；驗證規則與前端一致 |
| `database/repositories/password_auth.py`、`database/postgres/repositories/password_auth.py` | 呼叫點 + 既有 argon2id 寫法（與舊密碼雜湊相容） |
| `app/api/schemas/auth.py` | 前端 `lib/account-api.ts` 的欄位 |
| `tools/auth/google_login.py`、`tools/auth/line_login.py` | oauth routes；Google OIDC / LINE Login v2.1 官方端點 |
| `app/api/routes/email_registration.py`、`tools/auth/postgres_email_registration.py`、`database/postgres/repositories/email_registration.py` | API_ROUTE_INVENTORY 的路徑 + password recovery 的同型實作 |
| `app/repositories/knowledge_graph_repository.py` | knowledge sub-agent / explorer 的用法 |
| `tools/getzep/conversation_title_service.py` | orchestrator 呼叫契約 + Zep adapter |
| `tools/agent/response_composer.py` | `test/test_response_composer.py` |
| `agents/skin_tracking/extraction.py` | 4 個測試檔 + tracking agent |
| `agents/skin_tracking/composition.py` | stable 版本 + 目前的 PostgreSQL store |
| `database/postgres/migrations/0001_reconstructed_schema.sql` | 程式中全部 SQL（28 張表） |
| `scripts/rebuild_observation_catalog_from_neo4j.py` | 膚況標籤目錄 JSON 從 Neo4j `SkinObservationTag` 重建 |

驗證：所有內部 import 0 個未解析；離線測試 68 項通過（擷取器、部位規則、標籤升級、回覆潤飾器的事實保護、
認證規則、膚況狀態機）。需要真實資料庫與 GPU 的部分由 `verify_platform.sh` 在伺服器上驗收。

## 4. 已知缺口（上線前請處理）

1. **Email 註冊 API 的 request 欄位是推測的**：登入頁原始碼不在 bundle 裡。已同時接受 `request_id`/`challenge_id`、
   `password`/`new_password`。請用 Vercel 上的前端實測 `/api/auth/password/register/*`。
2. **會員資料（PostgreSQL）已遺失**：帳號、OAuth 綁定、測驗、膚況紀錄、簽到需要重新建立。
   - Neo4j Aura 仍保留遷移前的舊會員節點（`User`、`OAuthIdentity`、`PasswordIdentity`），可先執行
     `python scripts/preview_user_migration.py` 看數量，再評估匯回。
   - Zep 對話以舊 `users.id` 為 key；新帳號 UUID 不同，舊對話不會自動對上。
3. **遺失的資料檔**（非程式碼）：admin 商品審核用的 `product_*.json`、知識探索的 `knowledge_seed.json`
   （`KNOWLEDGE_GRAPH_ENABLED=0` 保持關閉）、台灣用語詞庫 `tw_*_100.json`。
4. **所有密鑰需重新產生或取回**：見 `.env.example` 的 `[REISSUE]` 標記。舊的 guest cookie 與登入 session 會失效。
5. `ADMIN_USER_IDS` 要改成重建後新帳號的 UUID。
6. `scripts/apply_daily_contents_migration.py` 與 `finish_dev_validation.sh` 仍指向不存在的 0003/0004 SQL；新 schema 已包含其內容。
