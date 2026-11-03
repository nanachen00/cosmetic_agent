# Staging 組員交接清單

狀態：等待組員完成外部環境；不得切換 production
日期：2026-10-02

## 全員共通規則

- 現有 api.blinkai.tw 到 8010 必須保持不變，不停止、不重啟、不替換。
- staging backend 只監聽 127.0.0.1:18110；18110 不開公網防火牆。
- 不在聊天室、Issue、截圖或文件貼密碼、Token、完整 DSN、私鑰、Cookie 或 OAuth Client Secret。
- 每項回報都要附：負責人、完成時間、環境 staging、預期 APP_RELEASE；不得附秘密值。
- 任何一項隔離驗收失敗，就停止後續步驟，不得進 production canary。
- staging 秘密由程序管理器或 source tree 外的受控檔案注入；不得改寫或複製專案內 production canonical .env。

## 1. Cloudflare／DNS 負責人

### 要做

1. 建立 staging-api.blinkai.tw，導向既有伺服器入口或 Cloudflare Tunnel。
2. 開啟 Cloudflare proxy 與 HTTPS；TLS 使用 Full strict 或團隊既有的嚴格模式。
3. 加 Cloudflare Access、團隊帳號白名單或固定測試來源限制。
4. API、SSE、livez、readyz 路徑關閉 cache；保留長連線。
5. 若要驗證完整 OAuth，再建立 staging.blinkai.tw 前端 origin。
6. 提供自動 smoke test 可用的受限 Access service credential；用安全管道交付。

### 不得做

- 不修改 api.blinkai.tw 的 DNS、Tunnel route 或 production origin。
- 不直接公開 18110。
- 不把 staging 設成任何人可存取。
- 不把 Access token 貼在回報中。

### 回報格式

- hostname：
- DNS record 類型／proxy：
- 入口方式（一般 origin 或 Tunnel）：
- 存取限制：
- TLS 狀態：
- API／SSE／健康路徑 cache：
- staging 前端 origin（若有）：

### 驗收

- 未授權請求被阻擋，授權後能抵達 staging Nginx。
- api.blinkai.tw 仍為正式服務，正式流量未變。
- 外網不能直接連 18110。
- SSE 沒有被快取或合併。

## 2. Nginx 負責人

### 要做

1. 新增獨立 server_name staging-api.blinkai.tw。
2. upstream 只能是 http://127.0.0.1:18110。
3. 轉送 Host、X-Forwarded-For、X-Forwarded-Proto、X-Real-IP 與 request ID。
4. SSE 使用 HTTP/1.1、proxy_buffering off、proxy_cache off，設定足夠 read/send timeout。
5. 公網 hostname 預設拒絕 metrics；只有內部來源或專用 bearer token 可讀。
6. 先驗證 Nginx 語法，通過後只 reload Nginx；不重啟 backend。
7. reload 前後確認 api.blinkai.tw upstream 仍是 8010。

### 不得做

- 不把 production upstream 從 8010 改成 18110。
- 不加入任何 production 流量權重。
- 不對普通 SSE 強制 Connection upgrade。
- 不把秘密寫進 Nginx 設定。

### 回報格式

- Nginx 語法檢查：
- staging upstream：
- reload 時間：
- SSE buffering／cache／timeout：
- metrics 保護方式：
- production 仍為 8010 的確認：

### 驗收

- staging 未啟動時回 upstream unavailable，不可誤轉 production。
- staging 啟動後，授權 HTTPS 可讀 livez 與 readyz。
- 未授權 metrics 為 404 或 403；授權後為 200。
- SSE 首筆事件即時送出。

## 3. Staging 資料與帳號隔離負責人

### 要做

1. 建立獨立 PostgreSQL database blink_staging 與最小權限 app role；不要只換密碼。
2. 建立獨立 Zep project／tenant；交付 staging 專用 ZEP_API_KEY。
3. 建立獨立 Neo4j instance／database 與帳號；現有程式以 NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD 注入。
4. 使用獨立 Redis 127.0.0.1:6389；套用 deploy/staging/redis.conf。
5. 建立合成測試資料與專用測試帳號，不複製正式個資。
6. 若有物件儲存、向量資料、報告或檔案，使用 staging bucket／namespace。
7. 產生 staging 專用 CHAT_GUEST_SESSION_SECRET 與 METRICS_BEARER_TOKEN。
8. 由程序管理器注入秘密，設定檔放 source tree 外並限服務帳號讀取。
9. 先執行 scripts/verify_staging_isolation.py；只回報 PASS／FAIL 與欄位名稱。

### 必須注入的非 OAuth 設定

- APP_ENV=staging
- APP_RELEASE=唯一 release 值
- STAGING_CONFIRM_ISOLATED_DATA=1
- DATABASE_URL=staging PostgreSQL
- REDIS_URL=redis://127.0.0.1:6389/0
- ZEP_API_KEY=staging key
- NEO4J_URI／NEO4J_USER／NEO4J_PASSWORD=staging 資源
- CHAT_GUEST_SESSION_SECRET=staging secret
- METRICS_BEARER_TOKEN=staging secret
- STAGING_WORKERS=1、2 或 4

### 不得做

- 不讓 staging 讀寫 production PostgreSQL、Redis、Zep、Neo4j 或檔案空間。
- 不用正式姓名、Email、對話或 Token 作測試資料。
- 不允許 Redis 不可用時退回程序記憶體。
- 不直接修改 production Redis；正式 Redis 另走備份、還原與雙人覆核。

### 回報格式

- PostgreSQL 隔離方式與邏輯名稱：
- Redis 127.0.0.1:6389 持久化驗證：
- Zep 隔離層級：
- Neo4j 隔離層級：
- 合成帳號數量／用途：
- guard 執行結果：
- 備份與還原演練摘要：

### 驗收

- staging 寫入只在 staging 儲存層可見。
- Redis 停止後 readyz 失敗，應用不退回 memory。
- Redis 重啟後 job、admission 與 SSE events 可恢復。
- staging 測試不改變正式資料數量或更新時間。

## 4. Google／LINE OAuth 負責人

### 要做

1. 建立獨立 Google OAuth Client 與 LINE Login Channel。
2. Google callback 設為 https://staging-api.blinkai.tw/api/auth/google/callback。
3. LINE callback 設為 https://staging-api.blinkai.tw/api/auth/line/callback。
4. 登入完成只能導回 staging.blinkai.tw，不可回正式前端。
5. 產生 staging 專用 OAUTH_ONBOARDING_SECRET。
6. 使用 staging 專用 Cookie 名稱：
   - AUTH_SESSION_COOKIE_NAME=blink_stage_session
   - OAUTH_ONBOARDING_COOKIE_NAME=blink_stage_oauth_onboarding
   - OAUTH_LINK_COOKIE_NAME=blink_stage_oauth_link
   - CHAT_GUEST_COOKIE_NAME=blink_stage_guest_session
7. 設定 CORS_ALLOW_ORIGINS 與 SESSION_TRUSTED_ORIGINS，只列 staging 前端 HTTPS origin。
8. 設定 STAGING_REQUIRE_OAUTH=1 後重新執行 isolation guard。
9. 測登入、取消、登出、state 不符、callback 重送與 session 過期。

### 不得做

- 不刪除或覆寫 production redirect URI。
- 不使用 HTTP callback。
- 不分享 client secret、access token、refresh token 或完整登入回應。
- 不讓 staging 登入建立 production 使用者或 Cookie。

### 回報格式

- Google／LINE staging app 顯示名稱：
- 已註冊 callback URL：
- staging frontend origin：
- 測試成員限制：
- 登入／取消／登出／錯誤測試：
- Client ID 末四碼（不得回 secret）：

### 驗收

- Google 與 LINE 都建立 staging session。
- production Cookie 不會出現在 staging，staging Cookie 不會傳到 production。
- 修改 state 的 callback 被拒絕。
- production 登入流程仍正常。

## 5. Production canary／rollback 覆核人

### 先決條件

- staging 1／2／4 workers 全通過。
- SSE Last-Event-ID 重連與 worker 重啟恢復通過。
- livez、readyz、metrics 權限通過。
- 資料隔離與 OAuth 已簽核。
- build 有唯一 APP_RELEASE，且 staging 與 production 使用同一 immutable artifact。
- 正式 Redis 備份已在另一個 instance 做過實際還原。

### 要做

1. 保存現有 production release、啟動設定與版本清單，確認舊版可立即重啟。
2. 由 release manager 與第二覆核者同時在線。
3. 新版先成為獨立 candidate upstream，不原地替換 8010。
4. 依 5% → 25% → 50% → 100% 提升，每階段都要有觀察窗口與簽核。
5. SSE 採連線級固定 upstream；舊 worker 先停止接新流量，再 drain 既有串流。
6. 監看 5xx、timeout、p50／p95、首 token、queue wait、event-loop lag、SSE 重連、Redis latency／memory、admission 釋放。
7. 每階段跑登入、聊天、history、quiz、check-in、report、profile smoke test。
8. 任何資料錯寫、登入跨環境、readiness 連續失敗、5xx 或 p95 超標，停止擴量並 rollback。

### 不得做

- 不先停止 8010 再試新版。
- 不一次切 100%。
- canary 時不做破壞性 schema migration。
- 不以清空 Redis 或 job state 作回滾。
- 不只看首頁成功就宣告完成。

### 回報格式

- 舊／新 APP_RELEASE：
- 各階段比例、開始時間、觀察時間：
- worker 與 release 對應：
- p50／p95／5xx／timeout／SSE／Redis 摘要：
- 功能 smoke 結果：
- rollback 是否觸發與原因：
- 第二覆核者：

### 驗收

- 所有功能 smoke 通過，release 與部署清單一致。
- 指標未超過事先核准門檻。
- worker 重啟時工作可恢復或明確轉為 retryable，不會永遠 running。
- SSE 重連不重複已確認事件，也不遺失後續事件。
- rollback 演練能在約定時間恢復舊版且不破壞資料。

## 交付給我的資訊

組員不需要把秘密貼回來；只需回覆上述非敏感欄位。秘密由他們直接放入受控的 staging 程序環境。收到 1 至 4 的驗收結果後，才能啟動 deploy/staging/run-backend.sh，依序跑 1、2、4 workers 與測試腳本。
