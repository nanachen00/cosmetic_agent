# BLINK 專案整理與修復總回報

最後核對日期：2026-09-25
遠端專案：`/var/lib/jenkins/tomod/cosmetic-agent`

## 1. 結論

這一階段已完成「不改變既有產品結果」為原則的後端整理：統一正式啟停入口、拆出部分路由與服務、補齊可選套件邊界、移除原始碼中的敏感 fallback、更新測試契約與架構文件。

目前正式服務仍由 8000、8001、8002、8003、8010 組成；8200 是實驗性 vLLM，未納入正式腳本。本輪已完成一次受控正式重啟與 production smoke test；沒有刪除仍有疑慮的程式。

最後一次完整程式測試基準（文件產生前）為：

- `pytest -q`：293 passed、1 skipped、10 subtests passed。
- Python compileall：通過。
- `git diff --check`：通過。

受控重啟已完成，目前 8010 已載入磁碟上的最新重構版本；正式結果與注意事項見本報告第 10 節。

## 2. 已完成的整理

### 2.1 正式啟停入口統一

正式來源只使用：

- `/var/lib/jenkins/tomod/ops/runtime/start-production.sh`
- `/var/lib/jenkins/tomod/ops/runtime/stop-production.sh`
- `/var/lib/jenkins/tomod/ops/runtime/status-production.sh`
- `/var/lib/jenkins/tomod/ops/runtime/restart-production.sh`

已統一的規則：

- 8010 是唯一 Backend + Auth。
- 8011 已退出正式架構。
- 只讀取 `/var/lib/jenkins/tomod/cosmetic-agent/.env`。
- 8004、8005 被標示為 retired，應保持關閉。
- 8200 被標示為 `EXPERIMENTAL / UNMANAGED`，正式腳本不會啟停它。
- 狀態檢查會核對 port、PID、命令、工作目錄、`/readyz` 與版本雜湊。
- 啟動流程會檢查 CORS 與 Google／LINE OAuth redirect。
- 停止流程在送出 TERM 前會驗證命令與工作目錄，避免停錯程序。

### 2.2 後端分層

本階段沒有把所有程式一次搬動，而是採低風險抽離：

- Welcome：`app/api/routes/welcome.py` + `app/services/welcome_service.py`
- Guest Session：`app/api/routes/guest_session.py`
- Conversation：`app/api/routes/conversations.py` + `app/services/conversation_service.py`
- Skin Record：`app/api/routes/skin_records.py` + `app/services/skin_record_service.py`
- Daily Check-in：`app/api/routes/daily_checkin.py` + `app/services/daily_checkin_service.py`
- Chat／SSE／Session clear：`app/api/routes/chat.py` + `app/schemas/chat.py`，原 Guard、額度與 admission queue 由主入口注入
- LINE／Google OAuth：`app/api/routes/oauth.py`，callback URL、Session 驗證與簽發由主入口注入
- Health、Auth、Quiz、Admin、Daily Content 等路由沿用 `app/api/routes/`
- PostgreSQL repositories 與外部 client 逐步和 API router 分離

已抽離的路由保持原本 URL、輸入、輸出、cookie 與錯誤行為。`api/api_server.py` 已降至 1,124 行且沒有直接 route decorator；它現在只負責相容 ASGI 組裝、共用安全依賴與 service/router wiring。

### 2.3 依賴與安全邊界

- `zep-cloud` 改成使用到功能時才載入。
- `psycopg` 改成使用 PostgreSQL 功能時才載入。
- 修正 PostgreSQL repository 的錯誤 self-import。
- Zep 不再使用原始碼中的憑證 fallback，只接受 `.env` 或程序環境。
- 因憑證曾存在舊檔或歷史紀錄，仍建議輪替 Zep key。

### 2.4 測試與契約

- Welcome 路由新增 2 個 focused tests。
- Guest Session 新增 3 個 focused tests，覆蓋既有 cookie、本機 cookie 與正式 Secure cookie。
- source contracts 已跟著路由抽離更新，避免測試只搜尋 `api_server.py`。
- Skin Tracking 測試改為符合目前產品設計的嚴重度 1–5 quick replies，並統一「中度」用語。

## 3. 前端功能與 API 稽核

本次只掃描前端原始碼 `app/`、`lib/`、`components/`，排除 `.next`、安全掃描產物、`node_modules`、checkpoint 與備份。共確認 54 個 API 使用位置。

### 直接由前端使用，必須保留

- 會員與登入：`/api/auth/*`、密碼註冊／登入／復原、Google／LINE 登入流程。
- 膚況測驗：`/api/quiz/questions`、`/api/quiz/submit`、`/api/quiz/latest`。
- 對話：`/api/conversations`、`/api/chat/history`、`/api/chat/message`、`/api/chat/message/stream`。
- 使用者資料：`/api/user/profile`、`/api/v1/profile`、`/api/v1/session/clear`。
- 每日追蹤：`/api/checkin/status`、`/api/checkin`、`/api/checkin/history`、`/api/daily-contents`。
- 膚況紀錄：`/api/skin-records/frontend`。
- Skin Tracking：`/api/skin-tracking/message`、current report、recommendations。
- 管理後台：overview、users、GPU、data health、knowledge、daily contents、cost summary 與 pricing。

### 前端未直接呼叫但仍必須保留

- `/readyz` 與 health：由啟動腳本、反向代理與監控使用。
- Google／LINE OAuth callback：由第三方服務導回，不會出現在一般 UI 按鈕的 fetch 清單。
- Guest Session 與 welcome：可能由初始化、反向代理或相容客戶端使用。
- Admin session、外部 callback 與背景工作端點。

### 需觀察後才能封存

- 已用目前 AST 與 route table 確認 skin-record／skin-report 各 operation 只註冊一次；legacy-compatible skin-record 與新 skin-tracking 是兩組不同用途的 API，沒有流量證據前仍不能刪除。
- `/api/v1/chat` 與新版 `/api/chat/message` 並存，前者可能是相容 API。
- 未出現在目前前端，但可能被排程、外部整合或舊版 mobile client 使用的路由。

結論：目前沒有足夠證據可以直接停用任何上述「未知」端點。本階段選擇保留並列為觀察清單，避免誤傷現有功能。

## 4. 備份與死碼盤點

以檔名 `.bak`、`.orig`、`.old`、`backup`、`.rej`、結尾 `~` 與 `.ipynb_checkpoints` 盤點，排除 Git、Node modules 與編譯目錄：

- 備份／checkpoint 類檔案總數：412。
- 已在 `ops/`、`backups/`、`cosmetic-agent-stable/` 等非主要執行區：230。
- 仍在主要工作樹的候選：182。
  - Jupyter checkpoints：123。
  - 具名備份檔：59。

本次未刪除這 182 個檔案，原因是前端目前另有未提交修改，部分備份可能仍是人工 rollback 依據。建議做法：

1. 產生 manifest（原路徑、大小、SHA-256、最後修改時間）。
2. 移到 `ops/archive/dead-code-YYYYMMDD/`，而非立即刪除。
3. CI、測試與正式驗收連續通過後保留 30 天。
4. 30 天內沒有 rollback／request log 命中，才做最終清除。

## 5. 效能問題的真實狀態

程式整理提高可維護性，但不等於已解決所有慢速與併發問題。目前已知真正影響體感的項目是：

- 單一 GPU 同時承擔大型生成模型與 embedding；若 8200 又載入第二份模型，會進一步占用 VRAM。
- 正式 embedding 8002 設為 `EMBEDDING_MAX_CONCURRENT=1`、queue timeout 2 秒；它保護 GPU，但高流量時會排隊或快速失敗。
- 先前商品向量搜尋曾量到約 31.5 秒，必須用分段計時確認耗時在 embedding、資料庫查詢、逐項重排或模型生成，不能只用「Neo4j 應該很快」推論。
- TAIDE、Router、LiteLLM 若形成不必要的多跳或重試，會放大首 token 時間。
- 串流可改善等待感，但不能把 31.5 秒的檢索本身變快。

目前程式配置仍明確使用 `taide-12b`／TAIDE 路徑；不能在未改配置與做實際 trace 前宣稱已全面切回 Terra。

## 6. 本次遇到的失敗與修復

產品／測試相關：

- Skin Tracking 舊測試期待空 quick replies，但目前產品已採 1–5 嚴重度按鈕；更新測試，不改 runtime。
- 舊測試期待「中等」，現行產品用「中度」；更新測試用語。
- Guest Session 抽離後完整測試失敗，原因是 source contract 只搜尋 `api_server.py`；修正契約，讓它同時檢查 router registration 與 route implementation。
- TestClient 每次 request 傳 cookie 觸發 deprecation warning；改為在 client 設定 cookie。
- 兩次 patch 的 hunk 計數／尾段不正確；均先在 dry-run 被攔下，修正後才正式套用，因此沒有留下半套修改。
- 重複 patch 被辨識後跳過，殘留 `.rej` 已清除。

稽核工具相關（未影響產品）：

- 舊 Jupyter 終端機停在未閉合的多行 shell 命令，為避免覆蓋使用者狀態，改用唯讀 Python console 稽核。
- 第一輪前端掃描誤包含 `.next*` 編譯產物，使結果膨脹到 1890 筆；第二輪限定 `app/`、`lib/`、`components/` 並排除 dot directories，得到有效 54 筆。
- 瀏覽器阻擋直接開啟 Jupyter contents API；改由 JupyterLab file editor 與 console 讀取，未繞過安全限制。
- 最終一致性稽核的前兩次唯讀腳本錯把四份架構文件定位在專案根目錄，因 FileNotFoundError 中止；沒有寫檔。定位到 docs/architecture 後重跑，五份正式文件無舊測試數字／OAuth 說法，且本機 Markdown 連結零遺失。

更細的逐次紀錄保留在 `docs/architecture/REFACTOR_LOG.md`。

## 7. 目前尚未完成／不可誤認為完成

- 已完成正式環境受控重啟，8010 已載入最新 refactor。
- 已完成 5 人 production 商品搜尋併發測試（5/5 成功、p95 15.999 秒）；先前 20 人測試為 20/20 成功、p95 16.051 秒。仍需長時間與混合情境負載測試。
- 31.5 秒舊商品路徑已用 production trace 重新定位；現行商品搜尋 5.628 秒，向量搜尋 0.930 秒、Neo4j enrichment 0.172 秒，主要延遲轉為 intent_parse 3.110 秒。
- AMD GPU exporter／Prometheus metric 名稱與 Admin dashboard 的串接仍需 AI-Stack 管理員權限確認。
- 8200 vLLM 還不是正式服務，不能依賴它提供穩定流量。
- 182 個 live-tree 備份候選尚未移除；本輪只完成安全分類。

## 8. 建議的部署順序

1. 確認 `.env`、OAuth callback、CORS 與 LiteLLM config。
2. 在專案目錄跑完整測試與 compileall。
3. 執行 `status-production.sh`，保存重啟前狀態。
4. 低流量時段執行 `restart-production.sh`。
5. 再執行 `status-production.sh`；必須確認 8000、8001、8002、8003、8010 均為 UP，`/readyz` OK，無 command／cwd／PID mismatch。
6. 做登入、測驗、一般聊天、串流聊天、商品推薦、每日追蹤、Admin 等 smoke tests。
7. 觀察 15–30 分鐘錯誤率、p95、GPU VRAM、embedding queue 與模型 timeout。

不需要固定每天重啟。只在部署新版本、設定變更、程序異常或確認有資源洩漏時重啟；正常情況應靠監控與健康檢查維持服務。


## 9. 最終驗收結果（2026-09-25）

- 架構索引、總回報、維運手冊、API inventory、依賴圖與逐次重構紀錄均可讀取；文件內容已同步最新 route/service ownership。
- 文件標題正確，未發現 private key、SECRET= 或 TOKEN= 等敏感標記。
- 最新完整測試：293 passed、1 skipped、10 subtests passed。
- Python compileall：通過。
- git diff --check：通過。
- 正式程序 8000、8001、8002、8003、8010 均為 UP；/readyz 顯示 application、PostgreSQL、Redis、TAIDE、TAIDE Router、Embedding 全部 OK；Nginx :80 正常；8004、8005 維持關閉。
- status-production.sh 現在回傳 0；runtime scripts、.env、LiteLLM config 與 API source 都符合 startup manifest，沒有 VERSION-MISMATCH。
- vLLM :8200 仍在執行，但維持 EXPERIMENTAL / UNMANAGED；restart-production.sh 不會管理它。
- 瀏覽器上傳本機 Markdown 曾被檔案選擇器拒絕；改用 JupyterLab editor 建立並儲存，再以遠端 console 驗證。此失敗未影響產品程式。

本輪沒有刪除未確認用途的路由、備份或 checkpoint；已完成受控正式重啟。

## 2026-09-25 最終效能與部署補充

- 明確 skin_consult 跳過重複 Terra 意圖判斷：12.297 秒降至 8.466 秒（約快 31.2%）。
- 已知膚況但只缺左／右部位時跳過重複 concern_assessment：8.420 秒降至 3.117 秒（約快 63.0%）。 正式 8010 API 為 4.348 秒（原 9.561 秒）。
- 20 人同時請求由 12/20 成功提升至 20/20 成功；p95 為 16.051 秒，排隊仍是主要延遲。
- 最新後端全套驗證為 293 passed、1 skipped、10 subtests passed；現行前端另以 tsc --noEmit --incremental false 重跑型別檢查並通過（3.77 秒、Git 狀態前後一致）。production build 本輪不重跑，避免在仍運作中的前端目錄重寫 .next。
- 現行前端 /var/lib/jenkins/tomod/frontend 是獨立 Git 工作樹，已有 16 筆既有修改；本輪未整理、還原或覆蓋。後端工作樹內舊 frontend 目錄的刪除紀錄不代表現行前端消失。
- 詳細數據請見 PERFORMANCE_AND_CONCURRENCY_REPORT.md。


## 10. 正式重啟、smoke test 與目前瓶頸（2026-09-25）

- 正式 restart 與 status 驗證通過；8000、8001、8002、8003、8010 全部 UP，PID／命令／工作目錄／版本一致，/readyz 全綠。
- 8200 仍為 EXPERIMENTAL / UNMANAGED，未修改或停止。
- Health 0.007 秒、Ready 0.034 秒、Guest login 0.009 秒、Guest reuse 0.002 秒；Admin 未登入 401 符合安全預期。
- 一般聊天 13.207 秒；最主要是 intent_parse 6.502 秒與 pain_gain_analysis 4.508 秒。
- 商品搜尋 5.628 秒；vector_search 0.930 秒、Neo4j enrichment 0.172 秒，主要延遲是 intent_parse 3.110 秒。
- SSE 串流 TTFT 0.799 秒、總計 12.985 秒；ingredient_extract 4.680 秒、Terra generation 6.995 秒。
- 5 人同時商品搜尋 5/5 成功，median 12.751 秒、p95 15.999 秒。多人延遲主要發生在 admission gate 之前的 agent/session 與 conversation-limit 存取，因此 queue_wait 會誤顯示為 0 ms。
- 下一輪高報酬改善：把 admission gate 前移；明確 flow 跳過重複 intent model；一般招呼／能力詢問走 deterministic fast path 並略過 pain/gain；快取或簡化 ingredient extraction；為前置 session／DB 階段補 p95 與 pool 指標。

