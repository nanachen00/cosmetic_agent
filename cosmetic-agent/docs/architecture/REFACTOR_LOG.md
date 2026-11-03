# Refactor Log

This log records behavior-preserving cleanup work, verification results, and issues found while reorganizing the backend.

## 2026-09-25 — Welcome route extraction

### Changed

- Extracted `GET /api/v1/welcome` from `api/api_server.py` into `app/api/routes/welcome.py`.
- Added `app/services/welcome_service.py` as the application boundary around the existing `get_agent(owner_id).get_welcome()` call.
- Added two route tests covering an explicit `user_id` and the existing `anonymous` default.
- Kept the URL, query parameter, owner resolution, agent factory, and response payload behavior unchanged.
- Updated the Skin Tracking assertions to match the already-active behavior: severity questions expose the existing 1–5 quick-reply buttons and use the existing label `中度`.

### Validation

- Focused welcome route tests: 2 passed.
- Skin Tracking service tests: 17 passed.
- Full repository suite: 275 passed, 1 skipped, 10 subtests passed.
- Python bytecode compilation: passed.
- `git diff --check`: passed.

### Issues observed

1. The first full run reported five Skin Tracking assertion failures. The production logic was not changed: the tests still expected empty quick replies even though the current flow intentionally returns the 1–5 severity buttons. The stale assertions were updated and the focused suite passed.
2. One remaining test expected `中等` while the current response and state use `中度`. The assertion was corrected without changing runtime code.
3. A duplicate local patch attempt was detected and skipped. Its generated `.rej` file was removed; no source change was reversed.

### Deployment status

- No production service was restarted.
- Running processes continue to use the previously loaded code until an approved managed restart.

## 2026-09-25 — Guest session route extraction

### Changed

- Extracted the active `POST /api/auth/guest-session` endpoint into `app/api/routes/guest_session.py`.
- Kept the signer provider, cookie name, TTL, `HttpOnly`, `SameSite=lax`, local-host Secure exception, cache header, and response payload unchanged.
- Added three route tests for an existing signed cookie, local cookie issuance, and secure non-local issuance.
- Updated the source-contract regression test so it verifies both the main application registration and the extracted route implementation.

### Validation

- Guest Session route tests: 3 passed.
- Source contract tests: 12 passed, 11 subtests passed.
- Full repository suite: 278 passed, 1 skipped, 11 subtests passed.
- Python bytecode compilation: passed.
- `git diff --check`: passed.

### Issues observed

1. The first Guest Session patch had incorrect unified-diff hunk counts and was rejected during dry-run as malformed. The counts were corrected before the source file was changed.
2. The first Guest Session test run passed but emitted a Starlette deprecation warning because a cookie was supplied per request. The test now sets the cookie on the client instance; the warning is gone.
3. The first full-suite run after extraction had one regression failure because the source-contract test still searched for the route decorator inside `api/api_server.py`. The contract now follows the route to `app/api/routes/guest_session.py` and verifies that the main app registers its router.
4. The initial source-contract patch also contained an incorrect final hunk count and was rejected during dry-run. The safe path/loading hunks were applied separately and the assertion block was replaced exactly; no partial or reversed runtime change occurred.

### Deployment status

- No production service was restarted.
- No endpoint was retired in this step. Unused-route decisions will be based on a frontend-to-backend usage inventory plus checks for background jobs and external integrations.


## 2026-09-25 — Frontend/runtime audit and final documentation

### Changed

- Audited active frontend source only (app, lib, components) and confirmed 54 API reference locations.
- Audited 68 backend route decorators and retained OAuth callbacks, readiness, guest, welcome, admin session, and compatibility routes that are not proven unused.
- Classified 412 backup/checkpoint-like files: 230 already outside the active tree and 182 live-tree candidates (123 checkpoints and 59 named backups). No uncertain file was deleted.
- Added PROJECT_REFACTOR_COMPLETION_REPORT.md and SYSTEM_ARCHITECTURE_AND_OPERATIONS.md.

### Validation

- Full repository suite: 278 passed, 1 skipped, 11 subtests passed in 2.24 seconds.
- Python bytecode compilation: passed.
- git diff --check: passed.
- Managed services 8000, 8001, 8002, 8003, and 8010 are UP; /readyz reports every dependency OK; nginx :80 is OK; retired 8004 and 8005 remain down.

### Issues observed

1. An old Jupyter terminal was stuck in an unclosed multiline shell prompt. The audit moved to a separate read-only Python console instead of overwriting the user's terminal state.
2. The first frontend scan included generated .next directories and produced 1,890 references. The source scope was corrected and the effective count is 54.
3. Direct browser access to the Jupyter contents API was blocked. The audit used the visible JupyterLab editor and console without bypassing the restriction.
4. Browser upload of the local Markdown files was rejected by the file chooser. The files were created in the JupyterLab editor and then verified from the remote console.
5. status-production.sh returns 1 because api/api_server.py changed after the last managed startup. This VERSION-MISMATCH is expected until an approved deployment restart; it is not a readiness failure.

### Deployment status

- No production service was restarted.
- No uncertain route, backup, or checkpoint was deleted.
- vLLM :8200 remains EXPERIMENTAL / UNMANAGED and is not controlled by restart-production.sh.

## 2026-09-25 最終效能階段與失敗紀錄

成功項目：

- 新增 skin tracking 分段計時。
- 明確 skin_consult 移除重複 Terra 意圖判斷。
- 已知膚況的缺部位追問改成本機固定文字，保留按鈕和資料結果。
- 啟動腳本 OAuth 檢查改為本機 8010 + Host/X-Forwarded-Proto，避免公開 tunnel loopback 誤判。

嘗試失敗但未造成資料損失：

- 遠端環境沒有 rg，改用 Python 唯讀掃描。
- 第一次封存命令卡在終端多行輸入，沒有搬動檔案；修正後才完成 99 檔封存與 hash 驗證。
- 遠端沒有 apply_patch，改採錨點數量驗證、暫存檔與原子取代。
- fuser 不存在，改用既有 PID 與工作目錄檢查。
- amd-smi 不支援 --memory 參數，改用可用的 process 輸出。
- 第一版串流測試缺 Idempotency-Key 得到 400，補上必要 header 後重測。
- 第一次皮膚測試指定錯誤路徑而未執行，改正路徑後通過。
- compileall 曾包含不存在的 services 目錄，改為實際目錄後通過。
- 三次 Jupyter 引號／換行編輯語法失敗，皆在寫檔前中止；以原子寫入完成。
- 第一次最終重啟服務皆 ready，但公開 OAuth loopback 回空值使腳本誤報失敗；改用本機 Host/proto 驗證後重啟成功。
- OAuth 診斷輸出曾因引號錯誤未執行，未修改任何檔案。
- 壓測後 API 情境測試命中 429 新對話限制；沒有關閉安全保護，改用已組裝的內部流程量測。
- 第一次內部 skin_record 測試未載入 composition，得到 skin_tracking_error；該 0.225 秒數字已作廢。載入正式組裝後為 8.420 秒，修改後為 3.117 秒。

仍需外部權限：AI-Stack AMD GPU exporter/Prometheus 設定，以及無法從容器對應的主機 GPU PID。

## 2026-09-25 API 使用證據保留

完成項目：

- 建立 API_ROUTE_INVENTORY.md，依正式 8010 OpenAPI 與目前前端靜態呼叫證據分類 70 組 method/path。
- start-production.sh 的 backend-8010.log 改為追加寫入，避免 managed restart 清空路由使用證據。
- 新增 scripts/report_api_route_usage.py，輸出 normalized route 的 requests、5xx、p95 與 last_seen；不讀取 request body、使用者訊息、token 或動態 ID。
- 新增單元測試；最終結果 2 passed，腳本 py_compile 與 start-production.sh bash -n 均通過。

本段失敗與修復：

- 第一版測試檔的換行跳脫被展開為實際換行，pytest 在 collection 階段以 SyntaxError 停止；沒有執行正式程式、沒有影響服務。重寫測試資料後重跑為 2 passed。

部署狀態：

- 未重啟任何服務；8200 未檢查、未修改、未停止。
- 追加日誌設定會在下一次核准的 managed restart 後套用；報告工具已可讀取目前既有 backend-8010.log。

## 2026-09-25 對話路由與服務層抽離

完成項目：

- 將對話清單、建立、改名、刪除與歷史 5 支端點移至 app/api/routes/conversations.py。
- 將 agent memory、session 清除、PostgreSQL 同步與 conversation limit 組合移至 app/services/conversation_service.py。
- api/api_server.py 保留相容 ASGI 入口與依賴注入，行數由 3621 降為 3358，直接端點由 23 降為 17。
- 新舊 OpenAPI 精確比對：對話三個 paths 完全一致；全域 method/path 均為 70，無新增、遺失或重複。
- 最終驗證：286 passed、1 skipped、11 subtests；compileall 與 git diff --check 通過。

本段失敗與修復：

- 第一次送出原子抽離前 Jupyter 連線中斷，介面未送入任何內容，沒有檔案異動；重新連線後才執行。
- 初次 OpenAPI 比對發現新增 tag 與刪除端點說明遺失；在全套測試前還原，重比結果零差異。
- 初次全套 pytest 為 285 passed、2 failed；兩個失敗都是舊來源契約仍要求 history 與 limit 字串存在 api_server.py。更新測試改驗證正式 route/service 位置後，全套通過。
- 獨立匯入測試顯示 opentelemetry.instrumentation.fastapi 未安裝；既有 tracing 採 fail-open，路由與測試不受影響。本段未擴大安裝依賴。

部署狀態：

- 未重啟 managed services；磁碟程式碼尚未載入目前 8010 process。
- 8200 未檢查、未修改、未停止。

## 2026-09-25 皮膚紀錄路由與服務層抽離

完成項目：

- 將 skin record 建立、清單、修改、刪除、目前報告與前端相容寫入 6 支端點移至 app/api/routes/skin_records.py。
- 將資料存取、payload 轉換、條件/區域映射與報告組合移至 app/services/skin_record_service.py。
- api/api_server.py 保留相容 ASGI 入口與 get_store/current user 依賴注入，行數由 3358 降為 3213。
- 6 個 operation 的方法、路徑與 OpenAPI 內容皆與目前 8010 完全一致；全域 OpenAPI operation set 仍為 70，無新增或遺失。
- 新增 2 個 service 測試；最終驗證為 288 passed、1 skipped、11 subtests，compileall 與 git diff --check 通過。

本段失敗與修復：

- 第一次全域路由驗證把 app.routes 的執行期系統端點與 live OpenAPI 相比，誤報新增 8 個既有端點（docs、openapi、metrics、health）。改用兩側 OpenAPI operation set 同口徑重驗後，70 對 70 完全一致；不是程式回歸。
- 第一次寫入本段紀錄時，Jupyter 內層換行跳脫被展開，腳本在 SyntaxError 階段中止，沒有修改檔案；改以 chr(10) 組合結尾後原子寫入。
- 測試輸出仍有既有 pkg_resources deprecated warning；不影響結果，本段未擴大更新依賴。

部署狀態：

- 未重啟 managed services；磁碟程式碼尚未載入目前 8010 process。
- 8200 未檢查、未修改、未停止。

## 2026-09-25 每日打卡路由與服務層抽離

完成項目：

- 確認每日打卡資料已由 database/postgres/repositories/checkin.py 寫入 PostgreSQL daily_checkins；舊 collection 名稱僅是相容介面，沒有 MongoDB 連線。
- 將 14 天週期、台灣時區、點數、防重複、Day 5/7 解鎖與歷史組合移至 app/services/daily_checkin_service.py。
- 將 GET /api/checkin/status、POST /api/checkin、GET /api/checkin/history 移至 app/api/routes/daily_checkin.py；登入身分解析仍由相容主入口注入。
- 移除錯誤寫成 MongoDB 的舊說明；沒有改資料格式、規則或回覆訊息。
- api/api_server.py 由 3213 行降為 2448 行；三個 paths 的 OpenAPI 與線上 8010 完全一致，全域 operation set 維持 70。
- 新增 2 個 service 測試；最終驗證 290 passed、1 skipped、11 subtests，compileall 與 git diff --check 通過。

部署狀態：

- 未重啟 managed services；磁碟程式碼尚未載入目前 8010 process。
- 8200 未檢查、未修改、未停止。

## 2026-09-25 核心聊天路由與 schema 抽離

完成項目：

- 將 POST /api/v1/chat、POST /api/chat/message、POST /api/chat/message/stream、POST /api/v1/session/clear 移至 app/api/routes/chat.py。
- 將 ChatRequest 與 conversation id 正規化移至 app/schemas/chat.py；保留舊 OpenAPI component key 相容標記。
- 主入口透過 ChatRouteDependencies 注入既有身分驗證、Guard、對話額度、併發排隊、Agent、路由映射與資源釋放函式；沒有改模型決策或 SSE 內容。
- api/api_server.py 由 2448 行降為 2066 行，直接 decorator 端點只剩 4 支 OAuth。
- 最終驗證 292 passed、1 skipped、10 subtests；compileall、git diff --check、全域 70 operations 與整份 OpenAPI paths 均和線上 8010 一致。

本段失敗與修復：

- 第一版抽離腳本因 Jupyter 多層換行字串在解析階段 SyntaxError 中止，發生於所有寫檔動作前，沒有檔案異動；改用 chr(10) 組合後原子寫入成功。
- 第一次局部測試 17 passed、1 skipped、1 failed；失敗是 conversation limit 測試硬性要求三次呼叫位於 api_server.py。更新為驗證主檔注入一次、chat router 呼叫三次後通過。
- 第一次聊天 OpenAPI 比對只有 ChatRequest component ref 因模組搬移而不同；class 建立後設定 module 太晚仍未生效，改在 class 定義內保留歷史 module key 後完全一致。
- 第一次全套測試 290 passed、1 skipped、3 failed；三項皆為 source_contracts 仍檢查舊檔案位置。更新契約同時驗證主檔 wiring 與新 router 的安全呼叫後，全套通過。

部署狀態：

- 未重啟 managed services；磁碟程式碼尚未載入目前 8010 process。
- 8200 未檢查、未修改、未停止。

## 2026-09-25 主入口與正式文件清理

完成項目：

- 從 api/api_server.py 移除 9 個 AST 確認未使用的匯入、重複 HTTPException 匯入與過時 append 操作說明；主檔降至 2054 行，AST 未使用匯入候選為 0。
- 更新 SYSTEM_ARCHITECTURE.md、SYSTEM_ARCHITECTURE_AND_OPERATIONS.md、DEPENDENCY_MAP.md、PROJECT_REFACTOR_COMPLETION_REPORT.md 與 KNOWLEDGE_AGENT_ARCHITECTURE.md，反映最新 route/service/schema ownership、PostgreSQL check-in 與 292 passed 基準。
- 修正總回報中「skin-record 路由重複」的舊判斷；目前 route table 顯示 legacy-compatible skin-record 與新 skin-tracking 各自只註冊一次且用途不同。
- 全部 architecture Markdown 本機連結驗證通過；歷史效能報告與 REFACTOR_LOG 的舊測試數字保留為時間點紀錄。

本段失敗與修復：

- 第一次架構文件更新在送往遠端前，因 Markdown 反引號與瀏覽器腳本 template literal 衝突而被工具解析拒絕；遠端沒有收到操作、檔案未異動。改用占位字元與 chr(96) 後原子寫入成功。

驗證：

- api_server.py py_compile、14 個關鍵契約與完整 OpenAPI paths 比對通過。
- 未重啟 managed services；8200 未檢查、未修改、未停止。

## 2026-09-25 LINE／Google OAuth 路由抽離

完成項目：

- 將 LINE 與 Google 的 login/callback 4 支 GET 端點原文移至 app/api/routes/oauth.py。
- api/api_server.py 僅注入 Session 驗證、callback URL、success URL、Profile URL 與 Session 簽發 helper；OAuth SDK、cookie 與 onboarding 行為維持原樣。
- 移除主入口 18 個已無用途的 OAuth 匯入；api_server.py 由 2054 行降至 1124 行，直接 route decorator 為 0，AST 未使用匯入候選為 0。
- 新增 OAuth route operation 測試；四個 OAuth paths 的 OpenAPI 與線上 8010 完全一致。
- 最終驗證 293 passed、1 skipped、10 subtests；compileall、git diff --check、70 operations 與完整 OpenAPI paths 均通過。

本段失敗與修復：

- 第一次送出抽離內容時 Jupyter 輸入框索引已失效，操作在輸入前被攔下，沒有文字送出也沒有檔案異動；取得新索引後重試成功。
- 文件同步先成功更新前三份文件，之後因總回報內 292 passed 出現 3 次而觸發單一命中斷言；總回報與 Knowledge 文件當時尚未寫入。改為明確驗證 3 次後完成剩餘更新。

部署狀態：

- 未重啟 managed services；磁碟程式碼尚未載入目前 8010 process。
- 8200 未檢查、未修改、未停止。

## 2026-09-25 最終一致性、前端與部署狀態稽核

完成項目：

- 以正確 docs/architecture 路徑檢查五份正式文件；沒有 2054 行、292 passed 或舊 OAuth 端點位置等殘留說法，本機 Markdown 連結零遺失。
- 後端 git diff --check 通過。後端工作樹仍有大量累積修改，未自動提交、還原或刪除。
- 確認現行前端位於 /var/lib/jenkins/tomod/frontend，為獨立 Git 工作樹且有 16 筆既有修改；本輪沒有更動這些內容。
- 前端以 tsc --noEmit --incremental false 驗證通過，耗時 3.77 秒，檢查前後 Git 狀態一致。為避免重寫現場 .next，本輪未重跑 production build。
- status-production.sh 顯示 8000、8001、8002、8003、8010 與 /readyz 均正常；PostgreSQL、Redis、TAIDE、Router、Embedding 皆為 OK。
- VERSION-MISMATCH 來自 start-production.sh 與 api/api_server.py 在 managed startup 後變更；本輪沒有重啟，因此磁碟版尚未載入現行程序。
- 8200 仍為 EXPERIMENTAL / UNMANAGED；未修改、未停止、未納入正式依賴。

本段失敗與修復：

- 前兩次唯讀文件稽核沿用錯誤路徑，假設 SYSTEM_ARCHITECTURE.md 等四份文件在專案根目錄，因 FileNotFoundError 中止；兩次都沒有寫檔。改先定位檔案，確認正式版本皆在 docs/architecture 後重跑成功。
- 初次全域 package.json 定位掃描包含 archived frontend、benchmark snapshots 與第三方 PyTorch tree，結果過寬；改為只讀取現行 /var/lib/jenkins/tomod/frontend/package.json。沒有刪除或修改任何掃描結果。

部署狀態：

- 未重啟 managed services；磁碟程式碼與啟動腳本待核准的低流量部署。
- 8200 未修改、未停止。

## 2026-09-25 正式受控重啟與 production smoke test

完成項目：

- 透過 ops/runtime/restart-production.sh 完成受控重啟；status-production.sh 回傳 0。
- 8000、8001、8002、8003、8010 的 PID、命令、工作目錄與 port owner 全部一致；/readyz 的 application、PostgreSQL、Redis、TAIDE、Router、Embedding 均為 OK。
- CORS、Google OAuth redirect、LINE OAuth redirect 啟動檢查通過；8004、8005 維持關閉。
- 8200 維持 EXPERIMENTAL / UNMANAGED，未修改、未停止。
- smoke test：health 0.007 秒、ready 0.034 秒、guest session 0.009 秒、guest reuse 0.002 秒；Admin 未登入得到預期 401。
- 一般聊天 200、13.207 秒；商品搜尋 200、5.628 秒；SSE 串流 200、TTFT 0.799 秒、完整 12.985 秒。
- 五人同時商品搜尋 5/5 成功；median 12.751 秒、p95 15.999 秒、max 15.999 秒。

本段失敗與修復：

- 第一次重啟時，一個伺服器內自我 SSH tunnel 使用 -L 8010:localhost:8010 搶占 8010；新 backend 因 address already in use 無法綁定，但舊啟動檢查只看到 port 開啟而一度誤判 READY。已只停止該衝突 tunnel 與卡住的重啟流程，確認 8010 釋放後重新執行正式重啟並成功。舊的 8011 tunnel 未占用正式 port，因此未擴大處理。
- 第一輪 smoke chat 沒有帶 Idempotency-Key，三支聊天請求在 2–4 ms 被安全層以 400 拒絕；不是模型或服務故障。補上必要 header 後一般聊天、商品搜尋與 SSE 全部得到 200。
- 遠端環境沒有 rg；測試入口盤點改用 Python 唯讀掃描，沒有修改產品檔案。
- 第一次寫入本段文件時，換行跳脫在 Python 解析階段造成 SyntaxError，發生於所有寫檔前；改用 chr(10) 與原子取代後完成。

效能診斷：

- 一般聊天 13.204 秒：intent_parse 6.502 秒、pain_gain_analysis 4.508 秒、agent_session 1.659 秒，是主要延遲。
- 商品搜尋 5.625 秒：intent_parse 3.110 秒；product vector_search 0.930 秒，其中 embedding 0.636 秒、candidate query 0.291 秒；Neo4j enrichment 0.172 秒。現在瓶頸不是 Neo4j。
- 串流 TTFT 0.799 秒、完整 12.985 秒：ingredient_extract 4.680 秒、Terra final generation 6.995 秒；兩次 Neo4j 查詢合計約 0.250 秒。
- 五人併發時，部分 agent_session / conversation_limit 在 admission queue 之前已耗 6.7–7.7 秒，但 queue_wait 記錄為 0 ms。現行 admission gate 太晚，無法保護前置資料與 session 工作，為下一輪優化重點。
- vLLM 測試 tmux 內有兩個已運作超過兩天的每秒監控迴圈，持續寫入約 920 MB CPU log 與 94 MB GPU log，合計 CPU 約 0.5%。因屬 8200 測試範圍，本輪僅記錄，未停止或刪除。

