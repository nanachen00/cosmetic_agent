# BLINK 系統架構、功能與維運手冊

版本：2026-09-25
適用環境：`/var/lib/jenkins/tomod`
文件地位：本檔是正式架構與維運的唯一主文件；其他架構檔只提供摘要或稽核證據，不得覆蓋本檔。

## 1. 一頁式架構

```text
使用者瀏覽器
    |
    v
Nginx :80/:443
    |
    +--> Next.js 前端
    |
    +--> FastAPI Backend + Auth :8010
              |
              +--> PostgreSQL：帳號、對話、測驗、紀錄、內容
              +--> Redis：共享執行狀態、session／限流相關狀態
              +--> Neo4j：Graph RAG／關聯檢索
              +--> BGE-M3 Embedding :8002
              +--> TAIDE Router :8001
              +--> TAIDE Model API :8000
              +--> LiteLLM :8003
              +--> Google／LINE OAuth
              +--> Better Stack／OpenTelemetry

vLLM :8200 = 實驗服務，不在正式啟停範圍
```

系統目前屬於「Web/API 模組化單體 + 獨立本機 AI processes」。FastAPI 不是把所有模型載進同一個 process，而是透過本機 port 呼叫模型、embedding 與 proxy。


### 1.1 實際程式目錄與責任

下列不是理想草圖，而是 2026-09-25 由正式主機檔案與線上 OpenAPI 交叉核對的實際結構：

```text
/var/lib/jenkins/tomod/
├── frontend/                         # Next.js 前端（獨立 Git repo）
│   ├── app/                          # 頁面與 route segments
│   ├── components/                   # 共用 UI
│   ├── lib/api.ts                    # 前端 API／SSE client
│   └── types/                        # TypeScript 型別
│
├── cosmetic-agent/                   # FastAPI／AI 應用（獨立 Git repo）
│   ├── api/api_server.py             # ASGI composition root，1,124 行，0 個直接 route decorator
│   ├── app/api/routes/               # 14 個薄路由模組
│   │   ├── auth.py, oauth.py, guest_session.py, email_registration.py
│   │   ├── password_recovery.py, chat.py, conversations.py, quiz.py
│   │   ├── skin_records.py, daily_checkin.py, daily_content.py
│   │   └── admin.py, health.py, welcome.py
│   ├── app/services/                 # 7 個業務服務
│   │   ├── conversation_service.py, skin_record_service.py
│   │   ├── daily_checkin_service.py, welcome_service.py
│   │   └── knowledge_history_service.py
│   │       knowledge_prompt_builder.py, knowledge_retrieval_service.py
│   ├── app/schemas/chat.py           # 已抽離的共用聊天 schema
│   ├── app/security/                 # request guard／chat guard／conversation limit
│   ├── app/clients/                  # 生成、embedding、knowledge response clients
│   ├── core/                         # admission、tracing、observability、usage tracking
│   ├── database/                     # PostgreSQL 與 repositories
│   ├── agents/                       # root／knowledge／product／skin_tracking／user_analysis
│   ├── tests/ 與 test/               # 回歸與功能測試
│   └── .env                          # 唯一正式 backend 設定來源
│
└── ops/runtime/                      # 唯一正式啟停入口
    ├── start-production.sh
    ├── stop-production.sh
    ├── restart-production.sh
    ├── status-production.sh
    ├── litellm/config.yaml
    ├── pids/
    └── logs/
```

線上 `/openapi.json` 共 70 個操作：auth 24、admin 13、skin-tracking 7、v1 5、skin-records 5、conversations 4、chat 3、checkin 3、quiz 3、daily-contents 1、skin-reports 1、user 1。

### 1.2 整理乾淨度結論

**結論：正式服務邊界與後端主要結構已整理乾淨並可運行，但整個 repository 還不是 100% 完成態。**

已完成並驗證：

- `api_server.py` 由 3,621 行降為 1,124 行，所有 HTTP route 已移至 `app/api/routes/`；線上仍保留 70 個 OpenAPI 操作。
- conversation、skin record、daily check-in、knowledge 等流程已開始分離到 service；AI HTTP 呼叫集中到 `app/clients/`。
- MongoDB 已退出正式 runtime 路徑；8010 是唯一 Backend + Auth，8011 已退休。
- 正式啟停只走 `ops/runtime` 四支腳本，backend 只讀 `cosmetic-agent/.env`。
- 後端完整測試 293 passed、1 skipped、10 subtests；compileall、前端 TypeScript noEmit、`git diff --check` 通過。
- 正式 restart、`/readyz`、登入、聊天、SSE、商品搜尋、Admin 權限與 5 人併發 smoke test 已完成。

仍需保留在待辦，不能假裝已清完：

- `api_server.py` 仍有 1,124 行，包含 dependency wiring、相容層與部分舊流程；可再小批抽離，但不應一次大搬家。
- `app/schemas/` 目前只有聊天 schema；其他 request/response model 尚未全面集中。
- 前端本輪只做介面與型別驗證，沒有大規模重排目錄，避免改壞現有畫面與 API 結果。
- 尚未取得 30 天流量證據的舊 API、備份與 checkpoint 只標記／封存候選，沒有冒險刪除。
- backend 與 frontend Git 工作區仍有累積變更，尚未依功能拆成乾淨 commits。
- admission gate 仍位於 session／conversation-limit 之後，多人時前置工作仍可能壅塞。
- 8200 由其他人員實驗，維持 EXPERIMENTAL / UNMANAGED，本輪沒有碰它。

## 2. 服務與 port

| Port | 服務 | 功能 | 正式腳本管理 |
|---:|---|---|:---:|
| 8000 | TAIDE model API | 文字生成，啟動檔 `taide_server_graphrag.py` | 是 |
| 8001 | TAIDE Router | `taide_router_8001.py` 模型路由 | 是 |
| 8002 | BGE-M3 Embedding | 把查詢／商品／知識文字轉成向量 | 是 |
| 8003 | LiteLLM | OpenAI-compatible proxy，讀取 runtime LiteLLM config | 是 |
| 8010 | FastAPI Backend + Auth | 對外 API、OAuth、對話、紀錄與 Admin | 是 |
| 8200 | vLLM Green | continuous batching 實驗 | 否 |

8004、8005 已退休；8011 已合併到 8010。

### 單 GPU 注意事項

8000 的大型生成模型與 8002 embedding 都可能使用同一張 AMD GPU。啟動腳本目前讓 8002 使用 `EMBEDDING_DEVICE=cuda`（ROCm 在 PyTorch 相容介面常仍顯示為 cuda）、最大併發 1、queue timeout 2 秒。

如果 8200 同時載入另一份大型模型，VRAM 可能出現約 30 GiB + 29 GiB + embedding 約 5 GiB 的多 process 佔用。8200 未正式切換前，不應讓正式流量依賴它；也不要以為 restart-production 會停止 8200。

## 3. 後端程式分層

```text
Request
  -> Middleware / Request Guard
  -> API Router
  -> Service
  -> Repository 或 Client
  -> PostgreSQL / Redis / Neo4j / AI Service
```

- `api/api_server.py`：相容 ASGI composition root；保留全域 guard 與各 router/service 組裝，目前沒有直接 route decorator。OAuth handler 已移至 `app/api/routes/oauth.py`。
- `app/api/routes/`：薄路由，負責 request/response、HTTP error mapping 與依賴注入。目前已包含 `chat.py`、`oauth.py`、`conversations.py`、`skin_records.py`、`daily_checkin.py` 等正式路由。
- `app/services/`：業務流程。目前已包含 conversation、skin record 與 daily check-in service；聊天先以依賴物件保留原流程，後續再逐段 service 化。
- `app/schemas/`：跨路由 request/response schema；`chat.py` 保留既有 OpenAPI component key 以相容舊客戶端。
- `database/.../repositories/`：PostgreSQL 等資料庫讀寫；check-in 由 `database/postgres/repositories/checkin.py` 存取 `daily_checkins`。
- `app/clients/`：集中封裝 chat completion、embedding 與 knowledge response 的外部 AI 呼叫；URL、timeout、header 與錯誤轉換不散落在 service。
- `core/`：設定、logging、observability 與共同基礎。
- `tests/`、`test/`：回歸、API 與功能測試。

抽離新功能時的原則是「路由要薄、服務負責流程、repository/client 負責 I/O」，但每次只移一小組並跑完整測試。

## 4. 主要功能怎麼運作

### 4.1 註冊與登入

支援 Google、LINE、密碼與 guest session。瀏覽器攜帶 HttpOnly cookie；正式環境 cookie 使用 Secure，localhost 開發例外。OAuth callback 由外部平台導回 8010，因此不能因為前端 grep 不到 callback 就刪除。

使用者操作：

1. 在登入頁選 Google、LINE 或密碼。
2. OAuth 成功後回到設定 username／平台頁面。
3. 忘記密碼使用 recovery request + confirm。
4. 登出／移除 provider 由 `/api/auth/*` 處理。

### 4.2 膚況測驗

前端取得題目、送出回答、讀取最近一次結果：

- `GET /api/quiz/questions`
- `POST /api/quiz/submit`
- `GET /api/quiz/latest`

結果寫入 PostgreSQL，後續可作為個人化對話與報告的 context。

### 4.3 對話與串流

主要端點：

- conversation list/create/rename/delete
- `GET /api/chat/history`
- `POST /api/chat/message`
- `POST /api/chat/message/stream`
- `POST /api/v1/session/clear`

一般功能流程：驗證使用者 -> request guard -> 讀取對話 context -> 決定意圖／知識／商品／膚況路徑 -> 檢索 -> 單一路徑生成 -> 儲存 -> 回覆。

程式位置：HTTP 與 SSE 入口在 `app/api/routes/chat.py`，request schema 在 `app/schemas/chat.py`，對話清單與歷史在 `app/api/routes/conversations.py`，組裝與既有 Guard/Admission 實作由 `api/api_server.py` 注入。

目前付費聊天實際順序是：

```text
security / request guard
  -> agent session
  -> conversation limit
  -> admission gate
  -> root agent / knowledge / product / skin path
  -> persistence
  -> HTTP 或 SSE response
```

這個順序有一個已確認的併發缺口：部分 request 在進 admission gate 之前，`agent_session`／`conversation_limit` 已花 6.7～7.7 秒，所以 `queue_wait=0 ms` 並不代表沒有排隊。下一輪應把 admission gate 前移到昂貴的 session／資料存取之前，再重測。

串流可把首批文字提早顯示；本輪 TTFT 為 0.799 秒，但完整答案仍需 12.985 秒。串流改善使用者感受，不能取代對 intent、ingredient extraction 與生成階段的實際加速。

### 4.4 商品與知識檢索

8002 將文字轉為向量；Neo4j／PostgreSQL 根據向量與條件找候選資料，再由流程組合 evidence 給生成模型。Embedding 不是回答問題，它只把語意轉為可比較的數字向量。

本輪 production trace 的商品搜尋總時間為 5.628 秒：

| 階段 | 時間 | 判斷 |
|---|---:|---|
| intent parse | 3.110 秒 | 目前最大單段瓶頸 |
| vector search | 0.930 秒 | 已從先前 31.5 秒問題顯著降低 |
| └ embedding | 0.636 秒 | 可觀察 queue 與快取，但不是主瓶頸 |
| └ candidate query | 0.291 秒 | 正常範圍 |
| Neo4j enrichment | 0.172 秒 | 目前不是慢的原因 |

因此「Neo4j 很快」與「整個商品回答仍要數秒」可以同時成立；目前應優先減少重複 intent 模型呼叫，而不是更換圖資料庫。

每次 request 仍應以相同 `request_id` 記錄 normalization、embedding queue/execution、vector query、Graph RAG expansion、filter/rerank、prompt build、model queue、TTFT、generation 與 persistence，不能只看總時間。

### 4.5 每日追蹤與膚況報告

前端目前使用：

- check-in status、submit、history
- daily contents
- skin record frontend adapter
- skin tracking message
- current report 與 recommendations

程式位置：skin-record HTTP 入口在 `app/api/routes/skin_records.py`、規則在 `app/services/skin_record_service.py`；check-in HTTP 入口在 `app/api/routes/daily_checkin.py`、14 天週期與解鎖規則在 `app/services/daily_checkin_service.py`。每日簽到實際寫入 PostgreSQL `daily_checkins`，舊 collection 形狀只是 repository 相容介面，不代表仍使用 MongoDB。

此區域仍有 legacy-compatible skin-record 與新 skin-tracking 兩組不同用途的路徑，但目前各自只註冊一次；修改或停用前仍要用 route table 與 request log 確認真正流量，不能因名稱相近直接刪除。

### 4.6 Admin

Admin 前端使用：

- Overview：使用者與系統摘要。
- Users：搜尋、查看、調整使用者狀態。
- GPU：讀取時窗內 GPU／VRAM metrics。
- Data Health：資料來源與資料庫健康。
- Knowledge：知識內容與檢索資料狀態。
- Daily Contents：列表、統計、修改內容。
- Costs：用量摘要與 pricing 設定。

若 GPU 圖表空白，不代表 GPU 沒有運作。AMD MI300X／ROCm 需要 AMD SMI／ROCm exporter、`/dev/kfd` 與 `/dev/dri` 權限、Prometheus scrape 和 dashboard metric/label 對應。只有 NVIDIA DCGM collector 時，GPU 圖表會沒有資料。

## 5. 模型路徑：TAIDE、Terra 與 vLLM

能確定的事實分成「服務有啟動」與「單一 request 真正走哪個模型」兩層：

- TAIDE 8000、Router 8001、LiteLLM 8003 都由正式腳本管理，設定中仍有 `taide-12b`；所以不能直接宣稱 TAIDE 已完全停用。
- 本輪 SSE knowledge trace 明確看到最終生成走 Terra，耗時 6.995 秒；這只證明該路徑，不代表所有一般聊天、商品與膚況請求都走 Terra。
- 最可靠的判斷方式，是每個 AI client 都記錄 `request_id`、flow、model、endpoint、queue time、TTFT、output tokens 與 fallback，而不是用「程序有沒有啟動」猜測。

合理的後續設計：

- 每個回答只保留一條主生成路徑，不固定做 Terra -> TAIDE 二次改寫。
- TAIDE 若保留，應負責明確可衡量的臺灣用語／在地知識功能，且失敗不影響主答案。
- vLLM 能以 continuous batching、PagedAttention 改善多人模型推理排程；不會自動修好慢 intent、慢檢索或 N+1 查詢。
- 8200 尚未切入正式流量；正式切換前必須使用同模型、同 prompt、同 token 上限、同併發做 A/B，並經負責人決定後才納入 start/stop/status。

## 6. 資料儲存

- PostgreSQL：帳號、identity、對話、測驗、膚況紀錄、每日內容等 durable data。
- Redis：session／限流／快取或共享 runtime state，實際用途依模組設定。
- Neo4j：Graph RAG 關聯與檢索；資料庫快不代表整條流程快，embedding、N+1 查詢、候選逐項處理都可能成為瓶頸。
- Zep：可選記憶體整合；採 lazy import，未啟用時不應阻止主 API 啟動。
- MongoDB：已退出目前正式服務路徑；歷史程式應維持封存，不應重新成為依賴。

## 7. 設定來源

唯一正式 backend env：

```text
/var/lib/jenkins/tomod/cosmetic-agent/.env
```

其他來源：

- LiteLLM：`/var/lib/jenkins/tomod/ops/runtime/litellm/config.yaml`
- PID：`/var/lib/jenkins/tomod/ops/runtime/pids/`
- Logs：`/var/lib/jenkins/tomod/ops/runtime/logs/`
- Version manifest：`runtime-version.sha256` 與 `runtime-version.meta`
- ASGI app：`api.api_server:app`

不要把 secret 貼到文件、Git、聊天或截圖。

## 8. 日常操作

先進入統一維運目錄：

```bash
cd /var/lib/jenkins/tomod/ops/runtime
```

### 查看狀態（安全、唯讀）

```bash
./status-production.sh
```

正常時應看到 8000、8001、8002、8003、8010 都是 UP，`/readyz` OK，並且沒有 COMMAND-MISMATCH、CWD-MISMATCH、PID-MISMATCH 或 VERSION-MISMATCH。

### 啟動

```bash
./start-production.sh
```

啟動順序：TAIDE 8000 -> Embedding 8002 -> Router 8001 -> LiteLLM 8003 -> Backend 8010 -> `/readyz` -> CORS/OAuth -> version manifest。

TAIDE 最長等待 240 秒、Embedding 180 秒、Router/LiteLLM 60 秒、Backend port 90 秒，之後 `/readyz` 再等待最多約 90 秒。大型模型載入時終端機看似停住是正常的，不要重複執行第二次。

### 停止

```bash
./stop-production.sh
```

停止順序：8010 -> 8003 -> 8001 -> 8002 -> 8000。腳本會驗證命令與 cwd；8200 不會被停止。

### 重啟

```bash
./restart-production.sh
./status-production.sh
```

重啟就是先完整 stop，再完整 start。執行期間網站會短暫無法回答；不要頻繁重啟，也不要在尚有大量請求時操作。

### 看 log

```bash
tail -n 100 /var/lib/jenkins/tomod/ops/runtime/logs/backend-8010.log
tail -n 100 /var/lib/jenkins/tomod/ops/runtime/logs/embedding-8002.log
tail -n 100 /var/lib/jenkins/tomod/ops/runtime/logs/taide-8000.log
```

需持續觀察時才使用 `tail -f`，看完用 Ctrl+C 離開；關閉 SSH／Jupyter 頁面不會停止由 nohup 啟動的服務。

## 9. SSH、Jupyter 與重啟腳本的差別

- SSH：連進主機的方式。
- Jupyter Terminal：另一種在瀏覽器內取得主機 shell 的方式。
- 啟停腳本：連上主機後實際管理服務的命令。

`31484` 是目前 Jupyter 網頁 port，不代表 SSH port。SSH 必須使用學校電算中心提供的 host、帳號與 SSH port。無論用 SSH 或 Jupyter Terminal，進入同一台機器後執行相同 runtime script，結果才會一致。

## 10. 故障排查

### Failed to fetch localhost:8011

8011 已退出正式架構。前端正式環境應經同網域 `/api` 或 `https://api.blinkai.tw` 連到 8010。修改 `NEXT_PUBLIC_*` 後需重新 build／啟動 Next.js；單純刷新頁面不一定生效。

### 回覆很慢

先看 stage timing，不要先重啟。2026-09-25 production 實測：

- 一般聊天 13.207 秒：intent 6.502 秒、pain/gain 4.508 秒、agent session 1.659 秒。
- 商品搜尋 5.628 秒：intent 3.110 秒、vector 0.930 秒、Neo4j enrichment 0.172 秒。
- SSE：TTFT 0.799 秒、完整 12.985 秒；ingredient extraction 4.680 秒、Terra generation 6.995 秒。
- 五人同時商品搜尋 5/5 成功，median 12.751 秒、p95/max 15.999 秒。

優先順序：

1. admission gate 前移，先保護 session、limit 與 DB pool。
2. 一般招呼／能力詢問走 deterministic fast path，略過不必要的 intent 與 pain/gain。
3. 同一 request 不重複呼叫 intent model；可判斷的商品條件先用規則解析。
4. 快取或簡化 ingredient extraction，限制 output tokens。
5. 補齊 model/endpoint/queue/TTFT/token 與 DB pool p95 指標後，再決定是否更換 serving engine。

只有當 TTFT 與 generation 隨併發明顯惡化，才優先評估模型 serving／vLLM；如果 intent 或檢索慢，換 serving engine 不會處理根因。

### GPU 顯示 0% 或無資料

先用 `amd-smi` 在主機確認 GPU 與 process；再查 AMD exporter、device 權限、Prometheus target、metric 名稱與 `goodagents` labels。Admin／Better Stack 只有在 metrics pipeline 有資料時才會顯示。

### status 顯示 VERSION-MISMATCH

代表啟動後，runtime scripts、`.env`、LiteLLM config 或 API source 有變動。先確認變更來源；若是預期部署，跑測試後於低流量時重啟；不要只為了消掉提示而盲目重啟。

### 8200 占用大量 GPU

8200 是 unmanaged。先確認 PID、命令、cwd 與是否正在做實驗，再由負責人決定停止；正式 stop script 不會碰它。

## 11. 2026-09-25 正式環境驗證結果

| 驗證項目 | 結果 |
|---|---|
| `status-production.sh` | exit 0 |
| Managed services | 8000、8001、8002、8003、8010 全部 UP |
| Readiness | application、PostgreSQL、Redis、TAIDE、Router、Embedding 全部 OK |
| 版本／PID／命令／cwd | 一致，沒有 VERSION/COMMAND/CWD/PID mismatch |
| Retired ports | 8004、8005 關閉；8011 退出正式架構 |
| 8200 | 保持 EXPERIMENTAL / UNMANAGED，未修改、未停止 |
| Backend tests | 293 passed、1 skipped、10 subtests |
| Frontend | TypeScript noEmit 通過 |
| 格式／檔案 | compileall、`git diff --check`、文件連結檢查通過 |
| Smoke test | health、ready、guest、一般聊天、商品、SSE、Admin 權限通過 |
| 5 人商品併發 | 5/5 HTTP 200；median 12.751 秒、p95 15.999 秒 |

本輪曾遇到的失敗也保留在 `REFACTOR_LOG.md`：第一次重啟被伺服器內自我 SSH tunnel 搶占 8010；只停止衝突 tunnel 後重新受控重啟成功。第一輪聊天 smoke test 漏帶 `Idempotency-Key` 而得到預期 400；補上 header 後全部通過。這些不是被刪掉的失敗紀錄。


## 12. 部署與驗收清單

以下是每次部署都要重新勾選的 reusable checklist，不是本輪歷史結果；本輪結果固定記錄於第 11 節。

- [ ] 確認 Git diff，只包含本次預期變更。
- [ ] 不把 `.env`、token、OAuth secret 放進 commit。
- [ ] 完整 pytest、compileall、typecheck/build 通過。
- [ ] 重啟前保存 `status-production.sh` 輸出。
- [ ] 低流量時執行 restart。
- [ ] 重啟後 status、`/readyz`、Nginx、OAuth、CORS 正常。
- [ ] Smoke test：登入、測驗、一般聊天、串流、商品推薦、每日追蹤、Admin。
- [ ] 觀察錯誤率、p95、GPU／VRAM、queue、timeout。
- [ ] 異常時依 manifest 與封存資料 rollback，不直接找任意 `.bak` 覆蓋。

## 13. 文件來源與後續維護

- 高階摘要：`docs/architecture/SYSTEM_ARCHITECTURE.md`（只做概觀，不可覆蓋本手冊）
- 唯一正式主文件：`docs/architecture/SYSTEM_ARCHITECTURE_AND_OPERATIONS.md`
- 修改與失敗紀錄：`docs/architecture/REFACTOR_LOG.md`
- 本輪總回報：`docs/architecture/PROJECT_REFACTOR_COMPLETION_REPORT.md`
- API 使用與保留判斷：`docs/architecture/API_ROUTE_INVENTORY.md`
- 依賴圖：`docs/architecture/DEPENDENCY_MAP.md`
- 封存政策：`docs/architecture/ARCHIVE_POLICY.md`

任何 port、模型、資料庫或腳本變更，都要同時更新本文件與 version manifest；禁止另外建立一支未受管理的「臨時正式腳本」。


### API 使用量盤點

不需要重啟服務即可產生最近 30 天報告：

    cd /var/lib/jenkins/tomod/cosmetic-agent
    python scripts/report_api_route_usage.py --days 30

此報告用 normalized route 統計 requests、5xx、p95 與 last_seen。任何 API 停用或封存都必須同時符合 API_ROUTE_INVENTORY.md 的觀察門檻，不能只因前端搜尋不到字串就刪除。backend-8010.log 採追加寫入；應監看 logs 目錄容量，並只在核准的維護時段進行封存或輪替。

## 最終正式服務邊界（2026-09-25）

- 8000：TAIDE 模型服務（managed）。
- 8001：TAIDE Router（managed）。
- 8002：BGE-M3 Embedding（managed）。
- 8003：LiteLLM／Terra gateway（managed）。
- 8010：唯一 FastAPI Backend + Auth（managed）。
- 8004、8005：retired，必須保持停止。
- 8011：已移除，不再使用。
- 8200：vLLM 實驗服務（EXPERIMENTAL / UNMANAGED），三支腳本不啟停。

正式啟停只使用 ops/runtime/start-production.sh、stop-production.sh、status-production.sh；restart-production.sh 依序呼叫 stop 與 start。設定只讀 cosmetic-agent/.env。status 會驗證 /readyz、PID、命令與工作目錄；start 會驗證 Python 語法、依賴、CORS、Google OAuth 與 LINE OAuth。

一般不需要定期重啟。只有部署新程式／環境變數、服務不健康，或模型程序確認異常時才重啟；重啟期間 8000、8001、8002、8003、8010 會短暫中斷，8200 不受影響。
