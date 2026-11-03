# 派發後 Workflow 修正（hotfix-postdispatch，2026-10-29）

修正 `docs/reports/BLINK_post_dispatch_workflow_review_20261029.md` 的 L1–L4 與 O1。和 P+Sys1 是不同的 bundle，彼此不依賴。

|編號|問題|修正|檔案|
|--|--|--|--|
|L1|膚況→商品訴求對應表是亂碼（「保汉」「拗狘」「捧油」），有膚況紀錄的會員找商品時，亂碼會進搜尋條件與長期偏好|還原 9/19 版對應；補上 `itching`→舒敏、`pigmentation`→美白；「嚴重」也算高嚴重度。`acne_scar`（痘疤）仍不對應，待團隊決定是美白還是修護|`tools/a2a/skin_product_bridge.py`|
|L2|需求確認用子字串比對「是／對／好」，「不是」「不對」「沒有，我是乾肌」都被當成同意並直接推薦|整句都是肯定詞才算同意。只有否定（「不是」、按「❌ 需要調整」）時保留原需求並問要改哪裡（新 stage `pain_gain_confirm_adjust`，不呼叫 LLM）；否定加內容（「沒有，我是乾肌」）才重新解析|`agents/root/orchestrator.py`|
|L3|「我是什麼膚質？…嘴唇腫起來而且呼吸困難」先被基本資料查詢接走，沒有 119 指示|緊急症狀檢查移到基本資料查詢之前|`agents/root/orchestrator.py`|
|L4|composer 會改寫錯誤訊息（本機把「知識庫暫時維護中」改成捏造的醫療說法），而且只檢查有沒有漏掉事實，不檢查新增|系統錯誤／替身訊息不送 composer；改寫結果多出原文沒有的數字，或長度超過原文 2 倍＋80 字，就退回原文|`tools/agent/response_composer.py`|
|O1|knowledge 回答已由 knowledge agent 用 Terra 寫好，composer 再改寫一次，多一次 Terra 呼叫|`knowledge_query`、`symptom_solving` 預設不送 composer（每題少一次 Terra）。要恢復舊行為：`.env` 設 `RESPONSE_COMPOSER_SKIP_INTENTS=`（空字串）後重啟 backend|`tools/agent/response_composer.py`|

測試：新增 `test/test_postdispatch_fix.py`，並更新 `test/test_response_composer.py`（原本的測試改成明確讓所有 intent 都經過 composer，以繼續驗證保護規則；另加 5 個新測試）。

## 套用前須知

- `unzip` 只會建立 `ops/hotfix-postdispatch/`，不會改任何正式檔案。正式檔案只由 `hotfix.sh apply` 修改。
- 每個指令都先用 sha256 比對目標檔，判斷它是 10/21 restore v5 的版本（BASE）、已是修正版（APPLIED），還是兩者都不是（DRIFT）。
  - 有任何 DRIFT（伺服器上有人改過），就不改任何東西。同時把伺服器上的現行檔案和預期、實際 hash 打包成 `drift/drift-<UTC>.tar.gz`，請把它交給 Claude，讀取最新版、重做修正後再套用。
  - 已是修正版的檔案（例如先用手動指令塊更新過 L1）會略過，只替換其餘仍是 BASE 的檔案。
- `apply` 不會重啟服務。要讓修正生效，必須重啟 backend :8010。重啟期間（約 10–60 秒）backend 無法服務；目前 `api.blinkai.tw` 回 530，前端本來就連不到。
- P+Sys1：S1、S2 不受影響。不要在 S3 執行中套用。如果在 `shadowpatch.py init-shadow` 之前套用，shadow 複本會包含這次修正。`sys1-dispatch-v1` 的 6 個錨點在修正後的 orchestrator 上都驗證過仍唯一，overlay 也能套用並編譯。
- 如果平常啟動 backend 時有設 `BACKEND_WORKERS` 等環境變數，`restart-backend` 前要在同一個 shell 先設定好，因為它是用 `start-production.sh` 把 backend 補起來。

## 步驟（VM，root）

```bash
cd /var/lib/jenkins/tomod
unzip -o /path/to/blink-postdispatch-fix-20261029-r<版本>.zip -d /var/lib/jenkins/tomod   # 版本見 BUNDLE_MANIFEST.json
cd ops/hotfix-postdispatch

./hotfix.sh check              # 預期 5 行 BASE，state: all-base（手動更新過 L1 則是 1 行 APPLIED，state: mixed）
./hotfix.sh selftest           # 在 /tmp 的暫存複本上套用並跑 4 個測試檔（不含 .env），預期 selftest PASS
./hotfix.sh apply              # 再跑一次 check 與 selftest → 備份到 backups/<UTC>/ → 只替換 BASE 的檔案 → py_compile
./hotfix.sh restart-backend --yes   # 只停 pid 檔記錄、且命令列是 uvicorn api.api_server:app --port 8010 的程序
./hotfix.sh verify             # 預期 all-applied、「在 apply 之後啟動 ✓」、/livez OK、/readyz ready
```

selftest 會把 `cosmetic-agent`（不含 `.env`、log、快取）複製到 `$TMPDIR`（預設 `/tmp`），跑完就刪除。

團隊程式在多處寫死 `sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')`，複本如果直接執行，會改成載入正式環境的模組，並透過 `load_dotenv()` 讀到正式環境的 `.env`。r4 的 selftest 就是這樣：11 個 composer 測試失敗，是因為測到的是正式環境的舊版檔案。所以從 r5 起，selftest 會：
- 把複本裡的這些路徑改指向複本自己（只改暫存複本，正式檔案不動）。
- 跑完後檢查，只要有任何模組或 `sys.path` 來自正式環境，就判定 FAIL。
- 列出測試期間新增的環境變數名稱（只列名稱，不列值）。正常情況應該是 `none`。

預期輸出包含 `env vars added during the run (names only): none`、`isolation OK`、`96 passed`。如果 `/tmp` 空間不夠，可改用 `TMPDIR=/data/blink/tmp ./hotfix.sh selftest`（目錄要先建立）；`apply` 也吃同樣的 `TMPDIR`。

選用：`./hotfix.sh smoke --yes` 會在正式 backend 建立 1 個訪客對話，送出「我是什麼膚質？我剛擦完精華嘴唇腫起來而且呼吸困難」，預期 `stage=emergency`。這題在緊急攔截就結束，不會呼叫 Terra。如果被 CORS／Origin 擋下，可設定 `SMOKE_ORIGIN=<允許的前端網址>`。

請貼回 `check`、`selftest`、`apply`、`verify` 的輸出（以及 `smoke`，如果有跑）。

## 回復

```bash
cd /var/lib/jenkins/tomod/ops/hotfix-postdispatch
./hotfix.sh rollback                  # 用 bundle 內 base/ 的 10/21 版本還原，刪除本修正新增的 test/test_postdispatch_fix.py
./hotfix.sh restart-backend --yes
./hotfix.sh check                     # 預期 all-base
```

- `rollback` 只還原內容仍是修正版（APPLIED）的檔案，包括手動更新過的 L1。
- 寫回之前，會先確認 `base/` 裡每個檔的 hash 都是 10/21 版。
- 只要有任何檔案已被其他人改過（DRIFT），它就停止並打包現行檔案，一個檔都不覆蓋。
- `backups/<UTC>/` 是 apply 當下的原檔備份，只用來留存紀錄；rollback 不依賴它。

只想關掉 O1、保留其他修正：`.env` 設 `RESPONSE_COMPOSER_SKIP_INTENTS=`，再 `restart-backend --yes`。錯誤訊息不送 composer 的規則（L4）不受這個設定影響。

## 不在這次範圍

L5–L8 與其他優化（O2–O9）見審查報告。其中 `_analyze_pain_gain` 的位置（O3）屬於 P+Sys1 的錨點 H6，要等 P+Sys1 定案後再處理。
