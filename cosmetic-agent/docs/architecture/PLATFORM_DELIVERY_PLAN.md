# 平台建構、優化與驗收順序

更新：2026-11-17（Asia/Taipei）。這份文件記錄實際證據與尚缺項目；完成 build 不等於所有外部服務已驗收。

目前驗收結果如下；後文帶日期的舊狀態保留作為追溯紀錄。

本輪自主優化以「照片推論穩定與模型斷線恢復」為第一優先：已補回同一受驗證 MI300X SSH master 的 BGE forwarding；前端分析前先檢查模型、斷線保留照片與備註；照片代理限制上游大小、禁止重新導向並清除內部欄位；embedding 連線池等待有上限，非有限與零向量不進檢索。Mac 正式狀態新增前端 build 對齊與照片 API 檢查，避免只看網站 HTTP200。

Fresh CE／focal 對照已完成各140步與80張驗證：macro-F1 0.630379 → 0.627938；2000次配對來源群組 bootstrap 的95%差異區間 [-0.027190,+0.023690]，沒有改善證據。兩組原門檻皆未過、checkpoints 與 NFS 備份校驗通過，三個研究根已標記 NOT_SELECTED；未追加 seed、未讀 test、未轉 Mac 權重或替換正式模型。

本輪實際照片驗收保留一次失敗：同張公開照首次64.215秒得到 invalid_model_output；隨後 direct10.5017秒／GPU9.638秒、會員網站12.9522秒通過，過暗控制圖0.5194秒正確要求重拍。原服務只完成權重載入便 ready，未做真實照片暖機。已部署 warm-v3：固定公開 fixture 真實推論通過才 ready；內部僅記 tokens、EOS、時間上限與有效性，不公開原始內容。候選首次公開圖8.9905秒、正式切換後會員網站9.7666秒、過暗控制0.5711秒皆通過，臨時會員已刪除。這修補了 readiness 缺口，仍不能據此宣稱首次64秒的原因已獲證明，或所有長時閒置情境均已消除。

照片受管候選驗收及兩使用者並行實測均已完成：膚況知識完整回覆12.045秒（200個內容 HTTP chunks），商品完整回覆12.914秒（單一內容 chunk，不能宣稱真串流），皆成功且無 fallback／超時。僅兩筆樣本，不據此宣稱 p95 或全流量15秒保證。82項後端測試、10組前端契約、28項照片服務與14項通道測試通過；最終網站、build對齊、backend/readyz、BGE、Redis及照片API全UP。完整本輪證據在 `.runtime/reports/photo-improvements-20261117/delivery-status.json`。

後續方向依序為：擴大來源固定的照片與長時閒置驗收，分開統計成功、超時與格式失敗；檢查商品回覆的串流路徑；再補有可靠標註的稀缺特徵與真實手機／多膚色資料。長時流量與實際容器重建仍未完成。弱標註照片不冒充人工 gold，不進行大規模死碼刪除。


| 項目 | 最新實際結果 |
|---|---|
| Mac 網站／後端 | HTTP 200、2 workers；啟用文字主路徑使用 Terra，標題為 deterministic，已移除 TAIDE／舊 router 依賴 |
| GPU 模型 | 正式 Qwen 照片 8025、BGE 8002 均為獨立 Torch 2.13.0+ROCm7.1；只綁 loopback，以 SSH 呼叫；未部署未達品質門檻的研究 adapter |
| 新一輪 Qwen 訓練 | `qwen-app-cycle4-20261116-r3` 已完成 6/6：559 train／80 validation、840 次 optimizer 更新；完整網站 JSON 最佳驗證 F1 **0.6304**，三 seed 均未達門檻，沒有選出或部署 adapter；NFS 原件／對照副本 SHA 通過，依使用者選擇停止 Mac 全量備份 |
| 11/17 資料准入與後續對照 | 5 筆不確定弱標註全部隔離；8 張 MEMI 原圖＋8 個 PNG 仍僅候選。原 559 筆資料的 gold-only r2 已完成 140 步＋80 張驗證：macro-F1 **0.6175**，低於舊 B **0.6304**，原品質門檻未過，沒有追加 seed 或部署 |
| 照片功能 | 同批公開照片 12/12 通過、過暗重拍 2/2；受管重啟後另測原 cohort 單張通過，臨時會員均已刪除；真實手機／專家標註品質驗收仍缺 |
| BGE／GraphRAG | 已實測網站單輪成分及膚況問答；膚況經 BGE→Neo4j 向量命中→Terra 完整回答。文字生成為 Terra，沒有經過 vLLM；多輪、引用正確率與長時間流量驗收仍待完成 |
| 網站效能 | 已部署串流／歷史快取／無效生成修正；最終兩人同時測試：膚況完整回覆 8.18 秒、商品 7.37 秒。846 項後端測試通過；小樣本結果不代表全流量 15 秒保證，詳見下段 |
| 已儲存檔案 | 最終基線核對 **3,046 檔／41,386,019,602 bytes**；17 組 checkpoint 原件與 NFS 備份對獨立 Mac sidecar 相符。Mac 另於操作後重核 **147 檔／1,486,474,493 bytes**，SHA 全一致 |
| 舊服務 | 前輪曾停止未使用的 TAIDE／gateway／遠端網站；本輪盤點再次觀察到遠端 vLLM 與舊服務運行，啟動來源未確認。Mac 主聊天不使用它們，本輪沒有停止或變更 GPU 程序，沒有刪除模型／環境 |
| 容器實際重建 | **尚未執行**。目前只完成模型服務程序 stop/start、備份／代表性還原、完整 pre 基線與正式 post 指令；學校控制平面權限仍不足，imageID／PVC spec 尚無法取得 |

最終 pre 在學校 `outputs/rebuild-verification/20261116-final-security-v6/pre.json`，Mac 獨立副本為 `.runtime/reports/security-recovery-20261116/final-rebuild-pre-v6.json`，SHA-256：`0ea970f047ea10bf6db049890e1a6a098ecc4f0340d71a6919f19685f78bd892`。這是重建**前**的保存基線，不能當作重建後已通過。

本輪訓練新增的環境、來源、輸出與 checkpoint **不在上述舊 v6 基線內**。新來源為 NFS `blink-runtime/src/qwen-app-cycle4-training-20261116-v3`；獨立訓練環境為 `blink-runtime/envs/qwen-app-cycle4-secure-t213-r71`，45 個 approved wheels 全部從已核對快取安裝，`pip check`／MI300X matmul 通過。官方 Qwen3-VL-4B revision 與兩 shard SHA 沿用既有固定值，沒有下載新模型。

實際 GPU pilot 已通過：完整 assistant JSON（含 EOS、排除 prompt／image tokens）的全序列 CE 與 sliced CE 均為 1.6499570608、差 0；完整 JSON＋aux 的 LoRA 反向與 optimizer 更新成功，language／visual-merger 梯度皆有限且非零。這只是訓練工程驗證，不是準確率改善。新 checkpoint 每 25 optimizer steps 保存 adapter、optimizer、scheduler、資料順序及 Python／Torch RNG；NFS 不支援原生 no-replace directory rename，已以獨占目的目錄、manifest 最後提交並真 NFS CPU 還原測試通過。

共用 GPU 的實際問題也保留：r2 研究生成期間，同一公開照片的受控 API 請求超過 45 秒，停止研究後為 5.6521 秒。r3 僅改研究排程：150ms 探測照片忙碌狀態、前向／反向間讓路、研究生成在下一 token 中止後重試同圖；中止的半份輸出不入評分。公開範例在 pilot／研究生成期間分別為 5.7712／5.7519 秒，已觀察一次中止後重試；這不是硬 GPU 隔離或持續流量 SLO 保證。正式 Photo／BGE 程序沒有被重啟或替換。48 項新 CPU 測試通過。

新資料重查新增可用影像 **0 張**：七個稀缺標籤原 URL 均 404，其餘 62 個 redness-only 項目未下載，避免放大偏態；未將 SCIN AI review 當人工 gold。719 原切分未變，80 張 test 與歷史 48 張 test 繼續封存；新訓練入口不會讀取 test，且不會自動部署 adapter。

研究輸出在 `outputs/skin-vlm/qwen-app-cycle4-20261116-r3`；Mac 既有 checkpoint 副本在 `.runtime/reports/qwen-app-cycle4-20261116-r3`，以 `watch-status.json` 的逐檔 SHA 與 `complete` 為準。使用者選擇不繼續 Mac 全量備份，停止傳輸與 watcher，保留已完成副本及 partial，不刪除原件。此前 r2 pilot 已另核 12/12 檔（含續訓狀態）；r3 未完成的 Mac 副本不算完成，也不作為訓練交付的阻擋項。

本輪六組已完成，研究程序正常退出；訓練與驗證總牆鐘約 **3 小時 12 分鐘**。下表全為同一 80 張 validation 的**網站完整 JSON 四特徵 macro-F1**，不能與先前五布林任務的 0.764 直接比較：

| 配方／seed | LoRA rank | visual-merger LR | macro-F1 | JSON／物件排除 | 品質門檻 |
|---|---:|---:|---:|---|---|
| 官方 base | — | — | 0.5744 | 98.75%／100% | 未通過 |
| A：完整 JSON | 8 | 凍結 | 0.6078 | 100%／100% | 未通過 |
| B：JSON＋0.25 aux，20261116 | 8 | 凍結 | **0.6304** | 100%／100% | 未通過 |
| C：B＋visual merger | 8 | 3e-6 | 0.6118 | 100%／100% | 未通過 |
| D：C 提高容量 | 16 | 3e-6 | 0.6039 | 100%／100% | 未通過 |
| B：seed 20261117 | 8 | 凍結 | 0.6209 | 100%／100% | 未通過 |
| B：seed 20261118 | 8 | 凍結 | 0.5887 | 100%／100% | 未通過 |

語言 LR 均為 3e-5、2 epochs、accumulation 8。B 為診斷上最佳配方，三 seed macro-F1 中位數 0.6209、相對 base 中位增益 +0.046463，但每一 seed 都未通過絕對／逐特徵門檻，因此 `selected_research_adapter=null`、`test_evaluated=false`、`automatic_deployment=false`。增加 visual merger 或 rank 在這輪沒有顯示 macro-F1 收益，不據此宣稱所有資料或模型皆如此。

主要缺口仍在稀缺特徵召回：B 首 seed 的 dark_marks 為 5/16（31.25%）、flaking 為 5/14（35.71%）；其餘 B seed 的 flaking 僅 4/14、3/14。對同一選模 validation 做 2,000 次 paired source-image-cluster bootstrap，B 減 base 的增益 +0.055948、95% percentile CI **[-0.057157, +0.161743]**，包含 0。這不是獨立泛化或手機準確率證據；沒有 verified patient IDs。獨立 CPU 覆核確認六組的全部指標、gates、ID／image SHA、75 個 frozen source hashes 與 protocol 指紋一致；報告在 `analysis/final-cycle4-independent-review.json`。

NFS 收尾校驗已通過：7 組（含工程 pilot）共 **84 檔／1,895,819,450 bytes** 逐檔 SHA 一致；6 組正式候選的第二份 NFS 備份共 **72 檔／1,476,142,485 bytes** 亦完全一致。Pilot 沒有第二份 NFS 備份；同 NFS 的副本不等同獨立儲存。正式 Photo／BGE 程序身分與健康通過，沒有重啟或替換它們。Mac 全量副本依使用者偏好停止；不得將 NFS 通過改稱 Mac 已全數備份。容器重建後仍須核對同一持久卷與檔案 SHA；獨立 Mac 副本可多防整個 NFS 卷遺失，但不是目前訓練或沿用同卷復原的必要條件。

下一輪優先補具有可追溯授權、專家外觀標註的 dark_marks／flaking 與四特徵陰性皮膚樣本，另收具同意的真實手機資料做獨立驗證。繼續保留 source／hash、解碼／品質、OCR、exact／perceptual／mirror 去重、group holdout 與人工審核；不以 AI review 或單純增加 redness-only 圖片補成 gold。這輪沒有新增可用公開影像，也沒有以調低品質門檻完成部署。

本輪確定的冷啟動問題：v2 在載入模型後立即 ready，受控首筆查詢於原 2 秒 client 重現 `ReadTimeout`（2006.93ms）。v3 加入固定合成短句的 GPU forward、1024 維有限值／單位範數檢查，全部完成才 listen／ready；相同冷啟首筆 88.64ms 通過，正式首筆 79.96ms 通過，timeout 與重試沒有放寬。這是受控恢復驗收，不是全流量延遲保證；最初未保存底層原因的失敗紀錄也保留。

正式服務來源：Qwen `qwen-photo-serving-20261116-secure-v2`／控制器 `qwen-photo-control-20261116-v4`；BGE `bge-secure-serving-20261116-v3`／已審閱的 v2 控制器。正式操作配方在 NFS `blink-runtime/src/mi300x-operator-20261116-v2/commands.json`，指向已實機驗證的 `mi300x-ops-20261116-v3`。Linux 控制 Python 明確使用具 pidfd 能力的 `/usr/bin/python3`；NFS 上的模型 Python 環境保持獨立。啟動時驗證實際 MI300X、預期 NFS source／rw、模型與來源 hash；缺失時停止。

當次公告掃描：Mac Python 77、npm 156、新照片 GPU 45、BGE GPU 52 個套件均未發現已知公告。這不涵蓋整個 OS／所有 native library、歷史研究環境或整體 CIA 認證。尚缺的權重仍明列：optional TAIDE2602、歷史 `BLINK_Passed_Research_Adapter.zip`；沒有因新訓練或程序重啟而宣稱找回。

後續仍需：學校管理者實際重建後執行 post／模型 API／網站複驗；具同意與人工外觀標註的真實手機資料；SMTP／OAuth、完整多輪 LLM GraphRAG 與長時間實際 provider 壓測。沒有使用程序重啟替代容器重建，也沒有用公開照片 schema 通過替代辨識準確率。

## 1. 版本同步與基線

已透過 GitHub connector 比較 `ed6edb3` 與 `main`，並執行 `git fetch`、`git pull --ff-only origin main`；結果 `Already up to date`。基線為 `ed6edb397e00d037d65bd3c7f4182eb89a2f0436`。既有本機修改完整保留，尚未 commit 或 push；CI workflow 也是本機新增、尚未在 GitHub 執行。

## 2. 可重現的完整建構

已完成：Python 3.12 hash lock、77 套件相容性檢查、npm lock、正式 Next build，以及本機前後端 API 與 member-auth proxy 路由。

完整 Mac 重建入口是 `ops/runtime/install-mac.sh`；會安裝 lock 依賴、build、啟動 Redis，並使用唯一 `.env` 初始化本機 `blinkai` 資料庫與 canonical migrations。API key 不存在時不會下載模型或呼叫 API。

```bash
cd cosmetic-agent
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-full.lock
cd ../frontend
npm ci
cd ..
ops/runtime/validate-platform.sh
```

`validate-platform.sh` 停用 `.env` 自動讀取、使用測試值與停用 live tests。它跑 backend 回歸、影像契約、前端 source contracts、TypeScript、production build，並確認 build 的 `/api` 路由確實指向 Mac `8010`。

API key 使用唯一的 `cosmetic-agent/.env`。已完成一筆 Terra 真實受控案例；照片與 GraphRAG 唯讀驗收沒有額外呼叫付費文字模型。

## 3. Mac 執行服務與多 worker 正確性

Redis 8.10.2 已安裝並在 `127.0.0.1:6389` 回覆 PONG；資料在專案 `.runtime/redis`，AOF everysec，記憶體上限 256 MiB，noeviction。限流、admission、SSE job 與對話狀態都要求 Redis；失效時拒絕新工作。

對話狀態以 JSON 保存在 Redis，TTL 30 分鐘，保留追問、確認、產品集合、膚況草稿等跨輪欄位。每輪讀取共享狀態，避免不同 worker 各有一份過期狀態；清除／刪除對話時一併清除。Redis key 使用識別值 hash，內容包含短期對話資料，正式開放遠端 Redis 前仍需另外規劃存取與加密。

`.env` 已設定下次啟動為 2 workers。目前實際 Mac Backend 已使用 2 workers，並通過 readiness 與 guest SSE 功能驗證。壓測支持 1／2／3 workers 的路由與共享狀態，但真實多輪模型品質仍須 live 驗收。

```bash
ops/runtime/start-mac-redis.sh
ops/runtime/build-mac-frontend.sh
BLINK_RUNTIME_ROLE=mac ops/runtime/start-production.sh
```

如果已有服務運行，先確認 canonical PID 與實際程序一致後才 restart；不接管未知占用埠。學校目前以獨立 Torch 2.13.0+ROCm7.1 環境跑 BGE／Qwen；歷史 vLLM 環境保留，沒有原地升級或刪除權重。

## 4. 已執行的回歸與容量驗證

- Backend：756 passed、18 skipped、10 subtests passed；跳過項目包含需外部設定的測試，不能當 live 通過。
- 影像／資料 CPU：75 tests passed；部署控制 CPU：79 tests passed。
- 前端 source contracts、TypeScript、production build：通過。
- HTTP smoke：14 路由通過，含未登入 401 與模型缺失 503。
- 早期 Chrome UI smoke 驗證首頁、登入頁與照片頁；最新正式 build 另通過會員網站的公開照片 12/12，以及服務程序重啟後單張複驗。沒有上傳私人手機照片或計算臨床準確率。

獨立容量測試：

```bash
cosmetic-agent/.venv/bin/python cosmetic-agent/tests/load/run_capacity.py --rounds 3
```

此 runner 啟動自己的 Redis 與測試 API（隨機 loopback 埠），環境不含 production secrets，使用正式 `create_chat_router`、ChatGuard、Redis admission、job store 與 shared state。只替換 agent／profile／history IO。測試結束會停止自己建立的程序。

| Worker | 階梯正常請求 | 32 併發 p95 | 最大同時模型工作 | 96 併發超載拒絕 |
|---|---:|---:|---:|---:|
| 1 | 183/183 | 1173.0 ms | 8 | 56 次 503 |
| 2 | 183/183 | 1040.1 ms | 8 | 56 次 503 |
| 3 | 183/183 | 1102.5 ms | 8 | 56 次 503 |

每組階梯為 1／4／8／16／32 併發，各跑三輪；另驗證 duplicate=409、跨 worker SSE replay 僅生成一次、6 輪共享狀態、斷線 cleanup、超載後 active/queue/security lock 歸零、失聯 worker 遺留的 queue ticket 過期復原。最後關閉私有 Redis，驗證請求得到 503。

上表是一次短時 Mock 測量，不是 TAIDE／Terra 真實速度，也不是長時間容量保證。2 workers 是後續真實測試起點；3 workers 不因單次較快就直接升級。

JSON 證據位於 `.runtime/reports/capacity.json`，包含 source hashes、worker PID、各併發 p50/p95/queue/first-delta 與故障測試結果；這些產物已 gitignore。

## 5. API key 到位後的功能整合驗收

依以下順序跑，每一步失敗先修復再往後；不能只看 `/livez`。

1. SSH `root@140.119.24.206:30892` 已經過登入確認，目標具 MI300X 與 NFS 掛載。模型通道與遠端命令共用 control socket；Qwen 照片 API 另轉發 `8025`。
2. 更新 `.env`，以 2 workers 啟動，驗證 `/readyz`、透過前端的 API 和 cookies。
3. 登入／登出／註冊／重設密碼／OAuth：使用隔離測試帳號與 SMTP；驗證 cookie、CSRF、跨使用者權限。SMTP、Google/LINE callback 尚未 live 驗收。
4. 產品查詢：固定需求與商品集合，檢查條件抽取、價格、禁忌、排序、無結果與 timeout。
5. 多輪聊天：連續追問／確認／換話題／斷線續接，在不同 worker 保留語意；真實 Zep／資料庫存取亦需驗收。
6. GraphRAG：正式 BGE→既有 Neo4j 唯讀連線（SkinConcern 52 筆）→實際向量檢索（1 筆）已通過；修正膚況工具誤用 TAIDE embedding 名稱。完整網站多輪、可選 reranker 與有引用的 LLM 回答尚未逐項驗收，不能把底層查詢通過當成全鏈通過。
7. 膚況文字紀錄→報告→使用者確認→存檔；測試修正部位、嚴重度與日期，不重複寫入。
8. Qwen 官方權重新下載並核對 pinned revision 與官方 LFS hashes。獨立照片 GPU 環境與 Mac SSH 代理已通過公開樣本、實際 base64 上傳、會員登入、401/403、429 admission 與 cleanup 驗證。使用本機隔離會員 fixture，沒有完成 SMTP/Email 註冊驗證；私人手機照片與臨床準確度尚未驗收。舊 LoRA 沒有恢復；新訓練 checkpoint 另有實際產物。

## 6. Terra 品質與實際模型負載

已建立有版本 hash 的 10 題 controlled alias／JSON smoke 題庫與 provider evaluator。預設 Terra、dry-run；真實執行必須明確加 `--live`，每次最多 10 題、輸出每題 128 tokens、並行 1／2／4、無自動重試。沿用現有 clients。歷史 TAIDE 評估入口保留，必須明確指定且另備妥服務。

```bash
cd cosmetic-agent
.venv/bin/python scripts/evaluate_providers.py
.venv/bin/python scripts/evaluate_providers.py --provider terra --live --limit 1 --concurrency 1
```

已通過 dry-run；一筆 Terra 真實案例的 schema 與 exact match 均通過。真實評估記錄模型、prompt/case hash、schema rate、exact match、latency 與錯誤型別。此小題庫只驗證接口與受控正規化，不能代表全平台品質。既有 adapters 不回傳 usage，因此報告明示 token/成本未知，需配合供應商用量資料；不能用時間臆估費用。

下一步應擴充固定題庫至產品／多輪／圖譜引用／安全拒答，先做人工標準答案與盲評，再比較 Terra 的配置與回歸結果，避免拿模型互評當正確答案。實際容量則在 staging 做 1→2→4→8 併發階梯，量測完成率、first delta、p95、429/503 與 provider 限流；達到 timeout／錯誤門檻即停止升載。

## 7. 影像資料、訓練與評估

早期報告的 128 training parents 是來源影像群組，不能直接宣稱有 128 位獨立人物。現有約 0.896 macro-F1 只屬於痘痘 patch presence；不能外推為泛紅、深色痕跡、脫屑或臨床準確率。

沿用 `experiments/skin-vlm/core.py` 的 manifest／允許標籤、`prepare_data.py` 的群組與圖像 hash leakage 檢查、`evaluate.py` 的逐標籤 precision/recall/F1，新增完整 train/val/test/external 稽核：

```bash
cd experiments/skin-vlm
../../cosmetic-agent/.venv/bin/python audit_splits.py \
  --train <train.jsonl> --val <val.jsonl> --test <test.jsonl> --external <external.jsonl>
```

順序：收集具使用權資料→獨立人工標註／複核→真實 subject_id 與 device/skin-tone/capture strata→凍結切分與外部測試→資料稽核→基礎模型 baseline→LoRA→只以 validation 選模型／閾值→單次 frozen test／external 驗收。切圖與 augmentation 不算新增人物。沒有多類別代表性資料前保留實驗標示、低品質拒絕與確認後存檔；不承諾精確判病。

訓練環境與 vLLM 生產環境分離，checkpoint 存 `/mnt/goodagents/checkpoints`，另做 checksummed backup。不要在目前可用 vLLM 4.53.2 Transformers 環境直接安裝 Qwen3-VL requirements。

### 2026-11-15 實際 Qwen 訓練

這次更新的是 **Qwen/Qwen3-VL-4B-Instruct 的 LoRA 參數**，在學校 MI300X 執行。官方 revision 為 `ebb281ec70b05090aa6165b016eac8ec08e71b17`；兩個 safetensors shard 均已核對官方 LFS SHA-256，來源與校驗值記錄在 `ops/runtime/models.manifest.json`。這是新下載的官方權重；歷史 `BLINK_Passed_Research_Adapter.zip` 與舊 LoRA 仍未找回。

| 資料 | 實際新增與切分 | 可用範圍 |
|---|---|---|
| [ACNE04](https://github.com/xpwu95/LDL) 固定版本 | train 824 張來源影像／1648 個裁切；validation 64／128；test 96／192 | 原作者框選衍生的局部病灶存在任務，學術研究用途；不代表四類膚況或正常皮膚判定 |
| [SkinCon](https://skincon-dataset.github.io/)／[Atlas](https://atlasdermatologico.com.br/) 原圖 | 320 張，train/val/test = 224/48/48；核對原圖 MD5、記錄 SHA-256 | 原始專家外觀概念對應四個 tag；非商業研究，尚未由本地標註者複核，也不是手機自拍資料 |
| [SCIN](https://github.com/google-research-datasets/scin) | 128 張頭頸候選，附來源及物件版本與校驗值 | 全部待外觀複核，**0 張列入訓練 gold labels** |

固定來源影像及近似重複群組後才切分；沒有可靠人物識別資料，不能宣稱不同人物完全分離。ACNE04 另排除歷史使用過的 252 個來源群組。下載入口是 `research/download_acne04.py`、`research/download_skincon.py`、`research/download_scin.py`，均位於 `experiments/skin-vlm`。

已修復實際 ROCm 執行問題：單一 patch 的 Conv3d 改為數學等價的線性投影，並在 MI300X 驗證輸出與反向梯度。相同權重與驗證影像下，SDPA 產生全陰性的異常；本輪採 eager attention。原先 SDPA run 已標記無效，不能把更換 runtime 的改善算成訓練進步。

六組痘痘裁切訓練完成。eager 基礎模型 validation macro-F1 為 0.8337；最佳 rank 8 與 rank 16 均為 0.9218，依預設規則選參數較少的 **rank 8、learning rate 1e-4、語言 attention + MLP**（16,515,072 個可訓練參數）。保留測試集另由 `research/v3/finalize_cycle3.py` 評估，不能用 test 調參。四特徵另比較三組語言層與兩組含 visual merger 的微調；低 loss 或 JSON 合格不等於辨識準確。

192 個局部裁切的 locked test：base macro-F1 **0.8743**、選定 adapter **0.9062**（+3.19 個百分點）；陽性 precision 0.8273→0.8900、recall 0.9479→0.9271，FP 19→11、FN 5→7。通過預設研究門檻，但 1000 次來源影像群組配對 bootstrap 的增益 95% CI 為 **−1.02 到 +7.42 個百分點**，跨過零，不能宣稱已證明穩定改善。此結果只屬局部框選研究任務，並非完整照片、四類膚況或臨床準確率。

四特徵五組比較均完成，以下為同一 48 張 validation；不是 test 成績：

| 配置 | macro-F1 | JSON 合格率 |
|---|---:|---:|
| 官方 base | 0.5842 | 91.67% |
| rank 8／lr 5e-5／語言 attention + MLP／2 epochs | 0.6880 | 100% |
| rank 8／lr 1e-4／語言 attention + MLP／2 epochs | 0.6173 | 100% |
| rank 16／lr 5e-5／語言 attention + MLP／2 epochs | 0.6479 | 100% |
| rank 8／lr 5e-5／另加 visual merger／1 epoch | 0.5641 | 100% |
| rank 8／lr 5e-5／另加 visual merger／2 epochs | **0.6948** | 100% |

五組均未通過預設 JSON≥98%、各 tag precision/recall≥80%、macro-F1≥80% 的門檻。**沒有選出可部署版本，四特徵 test 沒有開封**。下一輪需要具授權、涵蓋真實手機拍攝條件的人物分組照片與獨立外觀標註；先完成 SCIN 待複核佇列，再另立資料／調參協定及外部驗證，不能以這次 test 的錯誤繼續調參。

原始訓練／資料／程式指紋與結果分別在學校的 `outputs/skin-vlm`、`checkpoints/skin-vlm`、`checkpoints/skin-app-sft`；每組另有 NFS 校驗備份，並複製到 Mac `.runtime/checkpoint-backups/20261115`。研究權重不會自動替換網站照片模型。

**11 組實際 adapter 已全部通過保存參數有限值與非零 LoRA B 檢查，含三組 visual merger 的實際非零參數。11 組 Mac 獨立備份逐檔 SHA-256 核對通過，共約 1.01 GB**，索引在 `.runtime/checkpoint-backups/20261115/backup-index.json`。舊 checkpoint 未恢復的事實不因新訓練完成而改變。

照片服務使用獨立 Python 3.11、Torch 2.9.1+ROCm6.4、Transformers 5.23.0、Pillow 13.0.0、filelock 4.2.10。`requirements-photo-linux-py311.lock` 與 native lock 記錄實際 wheel hashes；已在新環境重裝並通過 pip check 與實際 MI300X 檢查。Triton 官方索引沒有提供上游 SHA，已明確記錄本地量測 SHA，沒有假稱上游校驗。

新建的 Python 3.11 環境另通過完整 Qwen forward、backward、visual merger 非零梯度與 optimizer step。這只是相容性 smoke，沒有保存新 checkpoint，也沒有將合成控制影像算入訓練成績；本輪比較仍使用原凍結 Python 3.10 環境。舊研究環境保留 Pillow/filelock/Torch 已公告漏洞，只供固定版本研究重現，不能作為接收任意上傳照片的服務環境。

照片 API 只綁學校 `127.0.0.1:8025`，Mac 以 SSH 轉發及既有 `PHOTO_MODEL_BASE_URL` client 呼叫。`ops/runtime/manage-photo-model.py` 提供 start/status/stop，核對持久掛載與程序身分，拒絕接管未知程序；預設只載入已校驗的官方 base。公開樣本與 base64 上傳約 11–18 秒，會員登入、未登入 401、跨站 403、錯圖 400、並行忙碌 429 與名額釋放皆已驗證。8 個同時請求為 1 成功、7 個忙碌拒絕；這不是 8 個同時完成的吞吐量，也不是長時間壓測。沒有保存上傳圖片，回應仍要求使用者確認且禁止自動存檔。

目前受管 serving source 為 `/mnt/goodagents/blink-runtime/src/qwen-photo-serving-20261115-v2`；啟動時禁止 native core dumps，實際程序的 soft/hard core limits 均為 0。已透過受管 stop/start 與公開樣本再次驗證。這是程序重啟證據，**不是容器重建或整個 NFS volume 遺失後的還原演練**。

機密性已有 SSH loopback、會員與 Origin 檢查及暫存檔清理；完整性已有版本、來源、hash、凍結切分及 checkpoint 備份；可用性已有 admission、錯誤 cleanup、服務重啟與新環境重裝證據。**Torch 仍有已公告漏洞**，不能宣稱 GPU 套件稽核為零；限制本地 safetensors 載入只降低特定攻擊面，並未修補套件。參見 [checkpoint 公告](https://github.com/advisories/GHSA-63cw-57p8-fm3p) 與 [JIT 公告](https://github.com/advisories/GHSA-rrmf-rvhw-rf47)。實際容器重建後的 NFS/hash 驗證、手機照片外部驗證、SMTP/OAuth、完整 GraphRAG 與長時間 soak 仍未完成。

影像資料與訓練完整性有 48 個 CPU 檢查，GPU 投影等價另有 3 個實機檢查。CPU 檢查用獨立 `requirements-data-tests.lock`，避免在 Mac 後端安裝 GPU 訓練依賴；已加入 CI 定義，但尚未 push 或在 GitHub Actions 執行。

### 2026-11-16 公開資料擴充與完整清洗

本輪仍訓練學校 MI300X 上的 **Qwen3-VL-4B-Instruct**，Mac 繼續負責網站。未修改正式照片服務的權重，也未使用 Terra／TAIDE 代替影像訓練。

| 新增來源 | 實際取得 | 本輪用途與限制 |
|---|---:|---|
| SkinCon／Atlas 原圖 | 400 張，排除先前 320 張的來源 MD5 | 使用原專家外觀概念；保留原圖 MD5、SHA-256、CSV 指紋與非商業研究限制 |
| SCIN | 128 個新案例，排除先前 128 個 case ID | 保留 GCS 物件 generation、MD5、大小及 SHA-256；全部待外觀標註，不進入本輪訓練或評分 |
| [COCO](https://cocodataset.org/) 原圖 | 96 張 | 僅取原 metadata 的 CC BY／CC BY-SA 授權及無 person／已知動物標註的物件場景，逐張檢視；1 張人像／玩偶難辨而隔離，95 張作非皮膚控制，不能當健康皮膚 |

AcneSCU 需向原作者申請；ACNEDET 尚未核實授權／存取；DermaBench／DDI 另有資料協議。這些資料**沒有下載或宣稱取得**，沒有寄信、代簽協議或抓取來歷不明的鏡像。來源狀態記錄在 `experiments/skin-vlm/research/dataset_sources_20261116.json`。

先前 128 張 SCIN 已在不顯示來源診斷的接觸表上逐張由對話助理視覺複核，留下每張可見 tag、品質旗標、說明及明確的 `null` 不確定值：126 份不同圖片 bytes，93 筆至少一個 tag 不確定，31 筆可供後續人工參考的低信任候選。**這是 AI 複核，沒有變成人類專家 gold，也沒有任何一張因此混入訓練。** 新舊 SCIN 共 256 筆僅進入品質檢查／待審佇列。

新增 128 張也另完成逐張 **AI 影像品質複查**，不是外觀標籤 gold：44 筆需優先確認，63 筆明確為其他身體部位，不能統稱為手機臉部照片。確認一對相同 bytes，另有兩張不同角度、衣物及拍攝情境相似的照片，保守指定同一待確認來源群組；沒有因此認定人物身分相同。新增的 `--manual-groups` 將複查群組綁定每張 id／SHA，缺少成員或 bytes 改變會停止，不會默默忽略。

清洗程式為 `experiments/skin-vlm/research/clean_expansion.py`，實際完成：

1. 核對每張來源 hash、原始專家概念及標籤轉換；原圖不可變，不以檔名或診斷文字作模型輸入。
2. 完整 decode／verify，限制格式、檔案大小、像素、最短邊、動畫與透明度；標記低細節、曝光、對比、遮蔽、色偏與人工覆核問題。品質不足進隔離區，不補大圖片冒充合格資料。
3. 與推論一致地在記憶體中做 EXIF 方向與 RGB 轉換；不美白、改色或自動裁切。只允許凍結協定中的 train-only 水平翻轉。
4. 原圖 SHA、正規化 RGB hash、pHash／dHash 及鏡像檢查，共用重複群組。標籤衝突隔離；先前四特徵 val/test 及局部痘痘 test 均保護，不重新進入訓練。
5. Apple Vision OCR 掃描 1,069 個不同實體路徑；無掃描錯誤、無常見條件／外觀關鍵字命中，另用含四個關鍵字的合成哨兵確認能偵測。僅輸出命中與計數，不保存整份 OCR 文字；這項篩查不能證明所有間接文字洩漏都已排除。
6. 在模型評分之前凍結來源影像群組切分。既有訓練只留 train；新 val/test 各 64 張專家來源及 16 張物件控制，每個 tag 各至少 12 個專家來源陽性／陰性。

最終 **train 559／validation 80／test 80，共 719 張**；其中專家來源 624、非皮膚控制 95。另有 145 筆隔離、288 筆既有保留集紀錄、204 筆待審、4 筆待審重複；145 筆隔離中 96 筆是舊 320 原圖清單與既有 val/test 的預期重合，不代表新增 400 張發生 96 次洩漏。所有接受／隔離／重複／保留原因均逐筆保存。

新增 SCIN 品質複查與來源群組補件後，再以新程式快照重跑到 `skin-appearance-clean-20261116-manual-qa`：隔離 188、待審 161、重複 4、保護 288；**合格 719 張及 train/val/test 三份 hash 完全不變**。原訓練快照與協定沒有改寫，補件只處理原本就不參與訓練的 SCIN。對照證據為 `manual-qa-comparison.json`；影像雜湊檢查不能取代逐張複查或證明所有不同角度照片都屬獨立來源。

另以 `research/protect_acne_parents.py` 從 pinned ACNE04 parquet 取出 192 個保留裁切對應的 **96 張未裁切原圖**，重核來源三個 shard 在 pinned 公開鏡像記錄的 LFS SHA-256。只使用來源檔名、群組及圖片 bytes 選取原圖，不以標籤或模型預測選資料。再將原圖加入保護清單，重跑至 `skin-appearance-clean-20261116-parent-qa`：保護紀錄增至 384，沒有新增影響合格資料的重複，三份 split hashes 仍相同。對照證據為 `parent-qa-comparison.json`；這是裁切與原始來源的完整性檢查，不代表人物身分隔離或新的 test 模型評分。

清洗在另一個新目錄、不同 `PYTHONHASHSEED` 獨立重跑；三份 manifest SHA-256 完全相同：

| Split | SHA-256 |
|---|---|
| train | `08d6e9fa0ae581926abc3ab7372977cdb6e4c78011ddd0d299659d9d48f951ef` |
| val | `805710681e72f749165271c962bcd255eb9bba40cd88a71f0d1dad18b9decd6b` |
| test | `82aa7a68c3f837429574e290160b1f7527e156cee1ab2fbe3f62d3d8925fcdd2` |

正式清洗產物在 `/mnt/goodagents/data/training/skin-appearance-clean-20261116-r1`，重跑對照在 `skin-appearance-clean-20261116-repro-check`。Mac 證據在 `.runtime/reports/qwen-expansion-20261116`，含逐張 audit、重複對、OCR 哨兵、人工排除、來源指紋與切分資訊。

**尚未補齊的代表性限制：** 沒有可驗證 patient ID，群組只代表同來源影像／近重複，不能宣稱人物完全隔離。新 val/test 的皮膚圖均至少一個外觀 tag，沒有四特徵全陰性的皮膚圖；物件控制不能補足正常人臉假陽性驗證。膚色極端樣本很少、缺少拍攝裝置與真實手機自拍分層。這些缺口不能由 hash、資料增強或 AI 標註消除。

本輪預先固定四組 rank-8 LoRA：兩個 learning rate、class-weight ablation、另加低 learning rate 的 visual merger；每組 2 epochs，再對 validation 最佳配方補兩個 seed，共六組。使用獨立可重建 Python 3.11／Torch 2.9.1+ROCm6.4 環境，僅對五個 boolean 判斷 token 計 loss。GPU 實測切片 logits 與完整 logits 的 CE 差值為 0.0，且反向梯度與 optimizer step 通過；不將這個 smoke 算入品質成績。

五個 boolean 是新的研究輸出協定，**不同於網站原有含描述的 JSON**；其 baseline／成績不能直接與上一輪 0.6948 相減。另以原本 48 張開發 validation 執行網站 JSON 相容性比較，該資料不參與新配方選擇。只有三個 seed 全達各 tag precision/recall ≥80%、macro-F1 ≥80%、JSON ≥98%、物件拒絕 ≥90%，且無 seed 比 base 差、median 增益 ≥0.02，才開封新 test；原 48 張未開封 test 也只在相同條件下做補充評估，不據此再調參。

**六組比較已全部完成，程序 exit code 0**；每組 140 次 optimizer 更新，共 840 次，完整比較約 76.6 分鐘。研究輸出在 `/mnt/goodagents/outputs/skin-vlm/qwen-clean-20261116-r1`，以下是同一新 80 張 validation、同一五 boolean 研究協定，全部 JSON 合格率為 100%：

| 配置 | Seed | 四 tag macro-F1 |
|---|---:|---:|
| 官方 base | — | 0.7339 |
| 語言 attention + MLP，class weights，lr 3e-5 | 20261116 | 0.7412 |
| 同上，lr 1e-4 | 20261116 | 0.7313 |
| 語言 attention + MLP，不加 class weights，lr 3e-5 | 20261116 | 0.6656 |
| class weights，語言 lr 3e-5 + visual merger lr 3e-6 | 20261116 | 0.7433 |
| 最佳配方重跑 | 20261117 | 0.7640 |
| 最佳配方重跑 | 20261118 | 0.7594 |

最佳配方三 seed 的 median 相對 base 增益為 +0.02549，但三組都未通過絕對品質門檻，主要不足仍是深色痕跡與脫屑的 precision／recall。**沒有選出可採用的研究 adapter；新 80 張 test 與原 48 張 test 均未進行模型評分，沒有挑最高分 seed 取代預定的第一個 seed，也沒有替換網站權重。** 結論以 `results/selection.json` 為準。

另在原 48 張開發 validation、原網站完整 JSON 協定做相容性檢查：base macro-F1 0.6013、結構／欄位合格率 89.58%；上述最佳配方第一個 seed 為 0.5744、83.33%。不合格回應包含不支援的 tag／region，仍被原驗證層擋下。這是另一個協定與資料群的次要開發檢查，與新 80 張的分數分開解讀；它也顯示新 boolean 任務的小幅增益沒有轉移為網站可直接採用的改善。

六份新 adapter 已逐份驗證保存參數全為有限值、每份 252 個語言 LoRA B 張量非零；含 visual merger 的三份另各有 8 個非零視覺 LoRA B 張量。每份均有資料／程式／官方模型 revision／環境版本指紋，原檔與 NFS 備份逐檔 SHA 一致。**六份獨立 Mac 備份也全部通過 SHA-256，共 471,504,942 bytes**，索引在 `.runtime/checkpoint-backups/20261116/backup-index.json`；這是本輪新權重，不代表失蹤的歷史 adapter 已恢復。

Mac 完整證據在 `.runtime/reports/qwen-expansion-20261116`，包含 `results/selection.json`、原網站相容性結果、逐張與原圖 QA、`saved-parameter-audit.json` 及最終摘要。下一輪優先取得正常臉部與真實手機拍攝的獨立專家外觀標註、補齊人物／裝置分組，再以網站原輸出協定訓練與驗證；繼續增加相似的臨床身體部位照片不能補足這些缺口。

訓練後健康檢查：Mac 網站 HTTP 200、後端 ready、經 SSH 的 GPU 照片 API ready。這項結果與手機照片外部驗證、長時間併發壓測分別記錄；後兩項仍待完成。最終機器可讀摘要為 `.runtime/reports/qwen-expansion-20261116/delivery-summary.json`，學校 `outputs/skin-vlm/qwen-clean-20261116-r1` 也保存同一份摘要。

資料／影像 CPU 檢查擴為 **57 tests passed**，包括來源 hash 損壞、原保留集／鏡像洩漏、複核指定群組跨保留集與成員校驗、不可默默放大、待審隔離、凍結輸出不可覆寫、無效 JSON 不能算正確拒絕，以及類別權重／切分覆蓋檢查；CI 定義已更新，尚未 push／執行。

### 2026-11-16 資安、外部照片與重建驗證續作

本節區分實際驗證與待部署修補。**工作途中 SSH master socket 消失，Mac 的模型轉發也中斷；中斷原因未確認，不能據此認定容器已重建。** 以下遠端健康結果都是中斷前觀察，尚未重新確認目前可用性。Mac 仍使用既有正式 build，本輪新 build 尚未啟用。

照片 GPU 已另建獨立 Python 3.11 環境 `qwen3-vl-photo-secure-t213-r71`，使用 Torch **2.13.0+rocm7.1**、torchvision **0.28.0+rocm7.1**、triton-rocm **3.7.1**。已實際完成 hash-locked 安裝、pip check、MI300X 有限值矩陣運算、完整 Qwen 官方 base 載入及照片推論；未套用研究 adapter。中斷前已由受管工具切換正式 8025，並寫入 NFS 的 `outputs/runtime/photo-model.active.json`。最新加強的掛載檢查與 socket 釋放等待仍在本機，待重新連線後部署；不要把 workspace 與已部署版本混為一談。

版本依 [PyTorch 官方安裝表](https://pytorch.org/get-started/previous-versions/) 與 [JIT 漏洞修補公告](https://github.com/advisories/GHSA-rrmf-rvhw-rf47) 選擇，並以實際 MI300X 相容性測試確認。原本 Torch 2.9.1 仍受 [checkpoint 載入漏洞](https://github.com/advisories/GHSA-63cw-57p8-fm3p) 影響，不能只靠限制 safetensors 宣稱已修補。保留的歷史研究／vLLM 環境沒有被原地升級，也不代表已退役。

原生 wheel 分開記錄上游與本地校驗資訊：torchvision／triton-rocm 有官方 index SHA-256；這次 Torch wheel 的官方索引未提供 SHA，使用官方 HTTPS 下載後量測、固定於 native lock，**不宣稱取得上游 Torch SHA**。Mac 後端 77 個套件、前端 npm 156 個依賴、新照片環境 45 個套件的當次公告掃描均為 0 個已知發現；這個範圍不包含整個 OS、所有 native library 或仍在運行的舊共用 GPU 環境。

影像入口已補固定連線上限、header／body 總期限、重複 Content-Length／Transfer-Encoding 拒絕，以及成功／例外／斷線後的 admission 釋放。GPU 上傳圖片改為記憶體解碼，不再依賴崩潰後可能殘留的暫存圖片；原生 core dump 仍禁止。前端同時限制 4 個進行中請求、5 MiB JSON、20 秒讀取期限、每會員每分鐘 4 次及有限大小的限流記錄。這些是單一 Node process 的限制，不是多副本共用的分散式配額。

已固定一批 **12 張公開 SCIN 外部功能照片**：10 張可供臉部／局部皮膚分析，2 張經客觀門檻判定過暗。原圖與預處理 bytes 均有 hash，與 719 張訓練資料的來源／近重複群組隔離；同一批次可獨立重建出相同 hash。舊環境經真實會員登入與網站 proxy 的 12/12 HTTP／schema 通過，新環境經候選私有 API 同樣 12/12 通過，兩者均 2/2 正確要求重拍；測試會員已刪除。未登入 401、錯誤 Origin 403、需要確認／禁止自動存檔／回應不含 raw output 的契約也通過。

新舊結果的 status／品質原因 12/12 一致、tag 集合 11/12 一致，scope 與 tag-region 各 10/12 一致。**沒有專家 gold，不能判定差異孰優、計算可信外部 F1 或宣稱辨識品質改善。** 兩條不同傳輸路徑的延遲也不是受控效能比較。這是公開照片功能測試，不是實體手機相機、HEIC、瀏覽器 canvas、可驗證裝置／人物分層或臨床驗證；新網站啟用後仍需對同一批 12 張重跑會員路徑。真實同意使用的手機照片與人工外觀標註仍缺。

BGE 仍待遷移：舊 8002 與 vLLM 共用 Torch 2.8.0 環境，尚未宣稱修補完成。已準備獨立安全環境的 49 個純 Python 套件 lock 加 3 個原生套件、固定既有 BGE revision 與 10 個檔案 hash、候選 8027 API 及受管啟動流程。Sentence Transformers 選 5.7.0，依 [官方套件資訊](https://pypi.org/project/sentence-transformers/5.7.0/) 與 [本地模型程式載入修補公告](https://github.com/advisories/GHSA-jhr6-gm9c-rqjv) 核對；不在舊環境直接降升套件。新環境安裝途中失去 SSH，是否完成必須重新盤點。GPU 五句向量 parity、完整環境掃描與正式 8002 切換都尚未完成；不得拿 CPU fixture 測試替代。新 API 也修正舊 API 對 batch 只回第一筆的行為，正式 client 的單筆介面維持相容，批次仍需實機驗證。

Terra 入口已完成程式修補與離線測試：intent／guard／pain-gain／composer 使用既有 Responses client；標題預設為確定性產生，`ENABLE_TAIDE=0` 不再讓網站因舊 router／TAIDE readiness 失敗。`terra_config` 只代表設定存在，不代表已實際呼叫模型。**本輪尚未修改正式 `.env` 或重啟 Mac 套用這些變更**。新前端已在 `.next-security-20261116` 建構成功，rewrite 指向 Mac 8010；建構／啟動會一致驗證選用目錄，不覆寫現行 `.next`。舊 GPU 服務的退役工具預設只產生計畫，需新的程序身分與 idle 證據，不能沿用 SSH 中斷前 PID 直接停機。

持久化與重建已完成的證據：17 組 Mac 獨立 checkpoint 備份，合計 **1,486,474,493 bytes**，逐檔 SHA-256 與 NFS 原檔／備份一致。另實際將一組 9 檔、79,539,044 bytes 的 adapter 從 Mac 還原至全新 NFS staging，逐檔校驗通過；這是 **一組**代表性還原演練，不是全部 17 組重建。初步 pre 快照核對 2,415 個檔案、41,381,370,499 bytes，發生於安全環境切換前，不能當成最終重建前快照。錯誤 NFS source 與同一容器冒充重建的負向控制均實際拒絕通過。

最新驗證器會要求預期可寫 NFS、不同容器身分、同一 sentinel、模型／checkpoint／source／lock hashes，以及 active pointer 與實際正式 8025 的 source／Python 相符；子程序清除 Python／動態函式庫注入環境。最新工具修補仍待上傳為新版本，已部署的舊版工具不適合作為最終驗收入口。容器內無可用 runtime socket／管理工具；學校 service account 對 pod／deployment 的重建操作回覆無權限。**沒有進行真正的容器重建**；需學校管理平台或管理者執行，並先確認 image、PVC、SSH 恢復方式。主機 key 若改變，需由管理介面另行核對，不關閉 SSH host key 檢查。

| CIA | 已取得的本輪證據 | 尚未完成 |
|---|---|---|
| 機密性 C | loopback＋SSH、會員與 Origin、無圖片落盤／回應除錯內容、無 core dump、限流清理測試 | 新網站正式套用與整合複驗；所有歷史 logger／外部服務並未全面認證 |
| 完整性 I | 固定官方 revision、區分上游／本地 hash、17 組獨立備份、資料 holdout 保護、拒絕驗錯 active 環境 | BGE 實機 parity、最終 pre 快照、實際重建後全量比對；歷史失蹤 adapter 仍未恢復 |
| 可用性 A | bounded admission／期限、例外與斷線 cleanup、受管照片切換、代表性還原；下段記錄 Mac 正式切換與部分通道恢復 | 照片與遠端控制通道、BGE 正式切換、模型 API 重建恢復、長時間 soak／故障演練 |

本輪證據集中在 Mac `.runtime/reports/security-recovery-20261116`；公開照片與初步還原證據已另存 NFS，其後在 SSH 中斷期間產生的修補／報告尚未完成 NFS 備份。最新離線測試結果為 `offline-test-results.json`，建構產物與待部署 source 另外保存。CI 定義已更新，尚未 push 或在 GitHub Actions 實跑。

#### 同日後續：SSH 部分恢復與 Mac 正式套用

2026-11-16 07:22（Asia/Taipei）重新檢查：現有 SSH 程序確實連至 `root@140.119.24.206:30892`，只轉發 8000／8002，兩端健康檢查均 HTTP 200。**這條通道已連線成功**，但沒有 `/tmp/blink-mi300x-control` 控制 socket，無法透過它執行遠端盤點；另開的非互動驗證也未取得認證。8025 尚未轉發，照片網站入口仍回應 503。這次只驗證轉發服務，尚未重新核對遠端 MI300X／容器身分／NFS。

在保留現有模型通道的情況下，已完成一次 Terra 真實 control case：最多 128 output tokens、單一既有 alias／JSON 案例、schema 與 exact match 均通過。這不是壓測或整個 agent 工作流程的品質驗收。Mac 部署前另重跑全套後端：**750 passed、18 skipped、10 subtests passed**；前端 contracts 與 TypeScript 通過。

接著僅修改正式 canonical `.env` 的三個非機密旗標：`ENABLE_TAIDE=0`、`CONVERSATION_TITLE_LLM=0`、`NEXT_DIST_DIR=.next-security-20261116`，並經既有 `stop-production.sh`／`start-production.sh` 受管重啟。網站與後端 readiness 均 HTTP 200，依賴只含 application／Postgres／Redis／observability／release／Terra 設定／embedding，不再依賴 TAIDE／router；Mac legacy router 已停止。既有 Redis 與 **2 workers** 設定保留。本節較前面「尚未修改正式 `.env`／啟用 build」為切換前紀錄，以這段後續結果為準。

另經既有 `EmbeddingClient` 實際呼叫單筆合成文字，回傳 1024 維、全有限值、單位範數向量；這證明 BGE 請求可穿過目前轉發，不能代替 GPU／NFS 身分核對。最新影像／資料 CPU 全套 **75 tests passed**。

照片 8025／遠端控制連線仍需補齊；BGE 實機 parity、正式 GPU 服務遷移、相同 12 張照片的新網站會員路徑重跑、真正的容器重建依然未完成。證據為 `mac-cutover.json`、`mac-precutover-tests.json`、`terra-precutover-live-smoke.json`、`ssh-restored-embedding-smoke.json` 與 `current-local-health.json`，均在本輪 Mac 證據目錄，尚未因這次部分轉發恢復而自動備份至 NFS。

### 2026-11-16 23:19 網站效能修正與實測

已透過 canonical runtime 腳本部署 `.next-perf-20261116`，Mac 後端保留 2 workers；website／backend／readyz／Redis／embedding tunnel 均 UP。GPU 模型服務未重啟。本節取代前面仍標示網站效能修正待部署的歷史狀態。

本輪修正：

- 敏感 API 的共用 middleware 原本覆蓋 SSE 的 `no-transform`，使 Next 壓縮層緩衝整份回覆。SSE 現使用 `Cache-Control: no-store, no-transform`，保留隱私與其他 API 的壓縮行為；實際客戶端已收到多個逐步到達的內容 chunks。
- 一般 auto 知識／症狀路徑也傳遞串流 callback，完成後沿用已串流的回答；只在確實需要確認時呼叫 Pain & Gain，避免無用生成與重寫。
- 簡單、明確、單一的既有成分別名使用本地匹配；不確定、多成分與複雜問題仍由原模型抽取。膚況 embedding 改用既有連線池與原有期限，沒有快取私人問題。
- 已完成訊息以 memo 避免重複格式化，歷史只在已提交內容變動時寫入，維持會員／對話隔離。離線 200 則／240 畫面重播，序列化及寫入 243→3 次、CPU 中位數 54.362→2.009ms；此數字不代表瀏覽器繪製或端到端速度。
- 修正「乾燥脫皮／緊繃」只走 risk_rules、漏查膚況的問題，補入 6 個明確症狀詞。移除商品搜尋每次都執行、但網站沒有使用的 intro 模型呼叫，保留原結果欄位相容。
- Terra 回覆必須完整結束；截斷、錯誤、取消或缺少 completed 的串流不算成功，不靠降低完整性要求達到速度目標。

下表由實際 `localhost:3000` 網站 API 取得；計時從合法登入完成後的 chat POST 開始，包含路由／檢索／生成。首段文字與完整回覆分開記錄，不把 status bytes 當首字：

| 路徑 | 首段文字 | 完整回覆 | 樣本／同時請求 |
|---|---:|---:|---|
| 一般 auto 知識問答 | 4.09–5.38 秒 | 7.19–8.41 秒 | 2 筆／2 |
| 成分知識問答 | 4.77 秒 | 6.80 秒 | 1 筆／1 |
| 會員知識問答 | 4.57 秒 | 8.13 秒 | 1 筆／1；臨時會員已刪除 |
| 修正後膚況 GraphRAG | 3.50 秒 | 8.18 秒 | 1 筆／2；向量命中已確認 |
| 省略無用 intro 後商品查詢 | 7.37 秒 | 7.37 秒 | 1 筆／2；實際 3 張商品卡 |

商品目前仍是完整結果一次送出，首段文字時間不能當作逐 token 串流 TTFT。其他知識路徑已驗證漸進到達。一般／成分／會員數字取自首版效能修正後，最後一版另外修正膚況路由與商品 intro；最後兩列是該次部署後同時執行的量測。

修正前成分案例完整回覆 12.87 秒、auto 10.23 秒；舊壓縮串流會到完成才一次到達。省略商品 intro 前，兩人同時測得 9.75／12.61 秒。這些是小樣本、未控制上游變動的觀察，沒有據此宣稱固定百分比改善或 p95 SLA。初次膚況案例雖然 6.39 秒完成，卻漏走圖譜，已從有效膚況效能證據排除；修正後重新驗證，不能以漏查資料的較短時間宣稱成功。

最終後端 **846 passed、18 skipped、10 subtests passed**；前端 typecheck、contracts、效能測試與 canonical 正式 build 通過。最終膚況日誌確認 `neo4j_skin_concern` 770.43ms、一次向量命中、無 embedding／向量查詢錯誤，隨後 `final_generation_terra` 完整成功。只保存白名單 stage 欄位，沒有保存測試帳號、cookie 或原始問答內容。

主聊天由 `knowledge_sub_agent` 經 `KnowledgeRetrievalService` 查 Neo4j，膚況支路先呼叫 GPU BGE-M3，再交由 `gpt-5.6-terra` 生成回答。成分直接圖譜查詢可以不經 embedding。遠端存在 vLLM 不代表這條請求會經過它；獨立知識地圖功能旗標仍關閉，沒有將其視為已驗收。

彙整證據：`.runtime/reports/site-performance-20261116/final-summary.json`；最終雙人量測：`.runtime/reports/site-latency-final-concurrency2.json`；白名單檢索證據：`final-pipeline-evidence.json`；來源指紋與部署：`runtime-switch-final.json`。本輪是有界的 1／2 人實際 provider 測試，尚未完成長時間 soak、高併發、長對話、冷啟動分層、所有問題的品質／引用驗收或全站 15 秒保證。

### 2026-11-17 資料品質准入與原資料對照

訓練皮膚影像沿用公開原圖，沒有自行用生成式 AI 製作皮膚圖片。固定 train 559 筆中，496 筆為 SkinCon 來源專家概念標註轉換成網站 JSON，63 筆為 COCO 物件來源的非皮膚對照。來源專家標註不等於本地真人重新複核；既有資料也不全是手機臉部，不能據此宣稱手機外驗已完成。

SCIN 兩個候選池共有 256 筆、252 個不同影像 SHA。已重核來源、授權檔、GCS generation／校驗、解碼、品質、OCR、exact／RGB／perceptual／mirror／來源群組，以及 1,103 筆既有 gold／holdout 指紋；另加入 12 張先前公開外部照片的預計算指紋，保護索引共 1,115 筆。這些保護步驟不開封 test 圖片，也不代表已驗證人物身分隔離。

初步 30 筆 AI 候選中，排除其他部位及非本輪目標後為 7 筆；再排除撞外部 cohort 的 2 筆，研究用候選為 5 筆近照、0 張臉部。每張弱標註 loss weight 為 0.25，不是把它們改稱人類 gold。研究 `qwen-silver-cycle5-20261117-r1` 完成工程 pilot 後，第二次逐張 AI 視覺檢查才發現遮蓋漏檢及標籤不確定性；這個順序有誤。主線以 PID／start ticks／cmdline 核對後使用系統 Python 的 pidfd 停止研究，完整訓練停於 **28/142**，沒有驗證結果或新網站部署。

原 r1 資料、來源、checkpoint 保留，report 有 `data-quality-stop.json`，checkpoint／備份根目錄有 `DO_NOT_DEPLOY.json`；不可改標後續接。新 launcher／trainer 會在啟動或讀模型前拒絕品質失效批次的 resume。資料準備現在必須實核最後視覺證據檔 SHA、逐張 ID／image SHA、四標籤嚴格布林且一致、scope、AI 來源與品質旗標；不再只信第一次 `silver_candidate=true`。原 5 張經新 gate 全數隔離，`silver-quarantine-intake-v3` 的 accepted=0、training_launch_allowed=false。另對 21 張較清楚人臉完成兩次 AI 檢查，沒有四標籤全確定的一致樣本，仍未入訓練。兩次 AI 檢查不等於人類／專家認證。

MEMI-DS 固定 v1 的 8 張正面 JPG 與 8 個對應 PNG 共 **10,228,751 bytes**，已下載到 NFS 隔離區，16/16 原作者 MD5、SHA 與獨立重讀核對通過。這是新候選取得，不是四特徵 gold、逐張手機來源證明或辨識成績；未加入本輪訓練。

依使用者「沿用原進度較好資料、不讓垃圾進入」的要求，新增 `qwen-gold-recall-cycle5-20261117-r1`：原 559 train／80 val 與 manifest SHA 完全固定，不允許 silver 或新來源。沿 B-r8／lr 3e-5／aux 0.25／2 epochs／seed 20261116，只將 dark_marks、flaking 的**真值正例**輔助權重乘 2，負例、其他特徵、JSON loss、分母 7 與總目標分母 1.25 保持原規則。原 train 的 dark_marks 為 142 正例／417 負例，flaking 130／429。這是可能犧牲 precision 的召回探索，不是新增訓練圖片。

新 gold pilot 已在 MI300X 實際完成 1 step，同時涵蓋兩個稀缺正例，138 個完整 assistant JSON tokens 與 EOS，full／sliced CE 差 0；11 檔 checkpoint SHA 及 weights_only optimizer／scheduler／RNG 載入通過。主線核對 pilot 程序退出後送出完整訓練，但 resume 的 protocol fingerprint 檢查拒絕，full 為 0/140，不能稱完整訓練在跑。模型、資料、來源與環境核對一致；不同 Python hash seed 下，歷史 macro-F1 的 set 浮點加總出現末位差異，已重現兩種指紋。保留原防護、原報告與 checkpoint，修正為核對後使用固定原始報告的指標值，另開新 source／run，而非改寫舊指紋。歷史同 seed B 是舊對照，並非新鮮配對實驗；所有原品質門檻、test 封存與多 seed 要求保留，沒有自動部署。

來源查核將新連結分開准入：UniData 三站為同一來源，公開 45 張預覽未提供四特徵標籤；Nexdata 舊 4,788 規格與目前轉址的 26,090 版本分開記錄，商業條款未取得。兩個 Kaggle 分類集僅列待審候選，卡片 license 不能代替原圖／標註／群組查核。FFHQ-Wrinkle 的 1,000 人工 mask 是 50,000 弱標註照片內的子集，任務主要是皺紋；Fitzpatrick17k 與現有 SkinCon 圖片可能重疊，不能重抓當獨立新增資料。

後續管線順序固定為：先定義四項外觀多標籤與輸出協定→來源／使用條件／解碼／品質／OCR／群組去重及標註複核→以來源群組固定切分，再做 train-only 增強→使用官方 Qwen processor 與受控 LoRA 對照→以 validation 做誤差分析，test 只做最後驗收→達門檻才評估 ROCm 推論效能與受管部署。白平衡、色彩擾動與 ROI 只作獨立對照，不默默改掉泛紅／深色訊號；不同裁切／鏡像必須留在同一來源群組。

後續已使用固定歷史報告指標建立穩定指紋，8 種 Python hash seed 一致，真正來源／數值變更仍拒絕。原 gold r1 的 protocol／pilot 保留並加 `DO_NOT_RESUME`，完整訓練仍為 0。新 source `qwen-gold-recall-cycle5-training-20261117-v2` 與新 run `qwen-gold-recall-cycle5-20261117-r2` 已實際通過 pilot 並開始完整訓練；最新進度以 report／`gold-recall-live-status-r2.json` 為準。訓練期間以既有公開第一張照片實測網站會員 API，HTTP 200／schema 通過、9.7214 秒、fixture 已刪除；只是一筆功能檢查，不是 accuracy／SLA 或 GPU 隔離證明。

MEMI 原生 Apple Vision OCR 已完成 8/8、文字／條件詞旗標為 0；僅表示該 OCR 語言／策略未檢出，不保證所有語言無文字。8 張對 1,115 保護指紋無 exact／near／mirror 命中；8 個 PNG 為多類調色盤索引，類別意義仍待核，不能直接轉成 dark_marks 金標。原圖與標註 bytes 未修改，尚未加入訓練。

證據集中在 `.runtime/reports/agent-data-intake-20261116`：`intake-summary.json`、`final-data-independent-check.json`、`source-acquisition.json`、`memi-intake-quality-v2.json`、兩份 `user-dataset-links-*-audit.json`、`gold-recall-data-review.json`、預先固定的 `gold-recall-ablation-plan.json` 與 pilot／live metadata。本機 66 項訓練／來源測試與 26 項資料測試通過；資料測試需使用支援 bit_count 的專用 Python，初次誤用系統 3.9 的失敗日誌仍保留。不把 pilot、loss 下降或 HTTP 200 當成模型品質通過。

**r2 最終結果已完成並獨立覆核。** 完整 140 次更新耗時 958.727 秒，16,515,072 個可訓練參數、visual 參數 0；80 張固定 validation 的 macro-F1 為 0.6174761312，舊同 seed B 為 0.6303793256，差 -0.0129031945。深色痕跡 TP 維持 5/16，FP 由 2 減為 1；脫屑 TP 維持 5/14，FP 由 1 增為 3。兩個目標召回率都沒有提升，JSON 與物件拒絕率均 100%，原品質 gate 仍失敗。

獨立程式重新核對 80 筆 ID／image SHA、raw JSON 與 envelope、全部混淆計數／指標／門檻，以及實際 83 個 source SHA。固定 2,000 次來源影像群組 paired bootstrap，新配方減舊 B 的 95% percentile CI 為 **[-0.0392550, +0.0135386]**，包含 0；這不包含訓練 seed 變異，也不是新鮮配對訓練／獨立人物／手機泛化證據。結論為未證明改善且不達原門檻，**不採用這組參數、不追加 seed、不開 test、不替換網站模型**，保留舊 B 作研究參照。

最終 checkpoint 為 manifest 列出 11 檔加 manifest 本身共 **12 檔／210,135,765 bytes**，原件與第二份 NFS 副本逐檔 SHA 一致。全部 LoRA 參數有限、252 個 B 矩陣非零；140 step、optimizer／scheduler／Python／CPU／CUDA RNG 已用 CPU weights_only 安全讀取驗證。只回傳 metadata，沒有重啟 Mac 模型備份。run 的 checkpoint／備份根目錄有 `NOT_SELECTED.json`，不能因檔案完整而當成可部署模型。

49 次服務健康探測皆通過；研究 worker 與 monitor 均已結束。最終摘要 `gold-r2-final-summary.json`、獨立比較 `analysis/gold-r2-independent-comparison.json`、checkpoint 覆核與 7 份小型收尾報告已同步學校 r2 report 的 `independent-review/` 並校驗。真實手機專家標註、新來源完整語意／權利驗證、合格模型及實際容器重建仍未補齊。MEMI 的 EXIF Make／Model 8/8 均缺失，不能從解析度猜手機來源。

## 8. 持續驗證與上線門檻

`.github/workflows/platform-validation.yml` 已準備好：SHA-pinned actions、locked install、離線回歸、正式 build、獨立 Redis worker 壓測。尚未 push／執行 GitHub Actions。

後續上線門檻：live 功能測試全通過→真實 provider staging 階梯→30–60 分鐘 soak→Redis／SSH／模型重啟故障復原→資料備份與 restore 演練→MI300X 重建後掛載／模型 hash 證明→小流量觀察。現在尚未完成這些 live／長時間／重建門檻。

CIA 以證據清單追蹤，不以未定義的百分比代表認證：機密性檢查 secrets/loopback/cookies，完整性檢查 locked revisions/hashes/權限與寫入一致性，可用性檢查 admission／故障復原／soak／還原。初次稽核的主觀百分比僅保留為歷史估計。

另修正 Root Agent 的 legacy stage logger：保留階段與 detail length，使用者輸入、profile 與 memory summary 不再直接 print。這是針對該 logger 的修正，不代表所有歷史模組已完成 log 資安認證。

## 9. 2026-11-17 推薦個人化診斷、依據與後續順序

本節是本輪商品推薦的新增政策與工作區修補紀錄，保留前面各輪的實際驗收歷史。**程式與離線測試通過不等於已完成本輪正式部署、個人化成效或真人使用驗收**；部署版本與網站量測須另留同版證據。

### 診斷與本輪修補範圍

推薦應先回答「是否符合這位使用者的需求與限制」，再比較合格商品。品牌知名度、很多人買、KOL 代言及新款會影響注意、熟悉感或信任，但不能取代成分來源、需求適配及個人實際使用結果。使用者表達的品牌偏好，也不等於客觀品牌品質或市場聲望。

本輪確認的資料問題是五條 Neo4j 商品查詢路徑未完整回傳成分證據、配方市場、推薦限制與價格來源；原查詢還把缺失的評價補成 `0.5`。目前 category／vector／partner／brands／identity 查詢已直接帶出 `ingredients_inci`、`inci_status`、`ingredient_evidence_level`、兩種成分來源 URL、`formula_market`、推薦範圍／限制、兩種適用膚質欄位、價格時間／來源／幣別及評論欄位，未知評價保留 `null`。這是保留既有節點資料，**沒有補造 INCI、評論、銷量或來源**；欄位存在也不表示所有商品已填妥。未找到正式 catalog 的獨立質地欄位寫入證據，不能宣稱已補齊質地資料。

此查詢子項新增 10 個契約案例，連同既有搜尋／效能／品類／幣別／排除／商品文案測試，當次共 **78 passed**。曾發現價格正規化把未知幣別填為 TWD，主線已修正為保留未知；外幣參考匯率換算及模型抽取但未獨立核實的網頁價格另標為估算，不能冒充已核對台灣售價，也不能通過硬預算。候選階段擴大測試曾為 192 passed；最終為 **211 passed**，前端 contracts、TypeScript、建置及聊天效能測試通過。最終部署與網站結果見本節末尾驗收紀錄。

偏好傳遞、候選硬限制、分數與卡片說明必須使用同一份本輪 context。明確當輪設定優先於舊偏好；推測的消費人格、一次點擊或照片外觀不能冒充使用者已確認的偏好。卡片應分別呈現推薦理由、資料缺口與來源，避免只給一個看似精確的「適合百分比」。

### 實際商品資料覆蓋率

2026-11-17 以既有 Neo4j 連線唯讀聚合，未讀會員資料、未修改商品。8,932 筆啟用商品中，19 筆通過完整 INCI／來源／配方市場文件門檻；結構化質地 0 筆、可辨識適用膚質標示 0 筆、有評分樣本數及可追溯來源的評論 0 筆。價格觀察時間只有 30 筆。名稱／描述中的質地字詞是待驗證標示線索，不能冒充實測質地。這些是欄位完整度，並非模型準確率或安全率。

啟用商品、可推薦商品、可顯示且有 embedding 的商品具有不同分母，不能混用；來源幣別推定也與明記 TWD／NTD 的當地價格分開。詳細計數、定義及當次程式雜湊留在 `.runtime/reports/recommendation-personalization-20261117/catalog-coverage.json`。此次查核顯示，**下一輪應先建立小批可核對的商品版本資料，再擴大推薦**；只調高個人化權重無法填補商品證據。過敏條件嚴格時可能沒有合格候選，應如實回覆資料不足，不放寬條件湊滿三款。

### personal-v3：可審查的初始政策

以下是本次指定的**初始工程政策**，不是由論文估得、不是醫療適合機率，也尚未證明為最佳權重：

| 分數維度 | 初始權重 | 用途與界線 |
|---|---:|---|
| `need_fit` | 35% | 當輪需求及已知膚質與可核對商品用途的對應；不把品牌或人氣當功效 |
| `ingredient_evidence` | 20% | 成分資料的完整度、來源及適用市場；不等於已證明有效或對個人絕對安全 |
| `texture_fit` | 15% | 使用者質地偏好與商品明確標示對應；資料不足須說明 |
| `budget_fit` | 15% | 已通過預算限制後，比較可核對價格；外幣估算不冒充當地售價 |
| `brand_preference` | 10% | 使用者明確喜歡的品牌；不使用預設品牌名氣或 KOL 光環 |
| `review_evidence` | 5% | 僅採有樣本數、評分及可追溯來源的評論；缺失不當中立好評 |

需求、質地、預算與品牌偏好合計 **75% 直接個人因素**，成分與評論證據合計 25%；只有具備足夠相關偏好時才能如此解讀。缺少某項使用者偏好時，停用該維度並對其餘啟用權重重新正規化，不能仍宣稱實際分配固定為 75%。商品資料缺件則保留資料不足與證據限制，不能為了算分而杜撰評價、質地或配方。舊 LLM 推測的五欄 `ranking_weights` 不得默默覆蓋新政策；只有明確帶 `ranking_policy_version=personal-v3` 的覆寫才能生效，避免舊 schema 導致質地權重變成 0。若使用者另調權重，結果需記錄實際使用值與政策版本。

**硬限制先於計分。** 已聲明過敏／避用成分、避用品牌、明確指定品類／品牌及有效預算，都不能靠其他維度補分通過。需要排除成分時，官方主打成分不等於完整 INCI；無法核對配方與市場來源時，不宣稱已完成完整過敏原排除。沒有合格候選便說明找不到或缺件，網路補查商品也必須走相同限制。品牌聲望、KOL、新上市與流行度不自動加分；不得從名稱猜代言、銷量或新品身分。

### 原始研究能支持什麼

可重查索引為 `.runtime/reports/recommendation-personalization-20261117/consumer-evidence.json`，含 9 個原始來源的設計、樣本、存取範圍、限制及後續假設；SHA-256 為 `73882e8d2339764cb95d396c650e8b0e63b8d9adb7718f4b22d6e5f10f3d720b`。以下不是本站已量得的推薦收益：

| 議題／原始研究 | 已核對的設計與結論 | 對本站的限制 |
|---|---|---|
| KOL 整體效果：[Pan 等，JAMS，2025，DOI 10.1007/s11747-024-01052-7](https://link.springer.com/article/10.1007/s11747-024-01052-7) | 251 篇、1,531 效應量；資訊價值、互動與品牌契合的作用隨產品／平台變動，區分意願、購買行為與銷售 | 原文明示多數研究為橫斷資料，不能據此推因果或給粉絲數固定加分 |
| 可信度與擬社會互動：[Han、Balabanis，Psychology & Marketing，2024，DOI 10.1002/mar.21927](https://openaccess.city.ac.uk/id/eprint/31583/) | 53 研究、250 效應量；感知專業、值得信賴、相似性與契合有關，結果有情境差異 | 感知專業不等於已驗證資格；購買意願不等於成交，不能直接外推台灣美妝 |
| 業配揭露：[Liu、Zhao，Marketing Letters，2025，DOI 10.1007/s11002-024-09757-z](https://link.springer.com/article/10.1007/s11002-024-09757-z) | 37 研究、12,721 人、288 效應量；摘要報告廣告辨識與品牌記憶增加，平均可信度下降，整體態度／意向效果不顯著 | 本次只核出版社摘要／註解；不能承諾揭露必提高轉換，也不能因此隱瞞合作關係 |
| 評論與實際銷售：[Babić Rosario 等，JMR，2016，DOI 10.1509/jmr.14.0380](https://pure.rug.nl/ws/files/81643765/The_effect_of_electronic_word_of_mouth_on_sales_a_meta.pdf) | 96 研究、1,532 效應量、40 平台、26 品類；客觀銷售平均相關 `r=.091`，評論量平均比正負向關聯強 | `r=.091` 不是銷量增加 9.1%；熱賣也可能帶來評論，不能移作權重或因果收益 |
| 評分的準實驗：[Luca，HBS Working Paper 12-016，2016 修訂](https://www.hbs.edu/ris/Publication%20Files/12-016_a7e4a5a2-03f9-490d-b093-8f951238dba2.pdf) | Yelp 顯示星等門檻的迴歸不連續設計，結合餐廳營收；作者報告獨立餐廳每星 5–9% 營收效果 | 是工作論文與美國餐飲的局部識別，連鎖結果不同；**不作美妝轉換率預估** |
| 大家都選的影響：[Salganik 等，Science，2006，DOI 10.1126/science.1121066](https://pubmed.ncbi.nlm.nih.gov/16469928/) | 14,341 人受控音樂市場實驗；社會資訊增加成功分配的不均與不可預測性 | 音樂下載不是美妝實購；人氣不是安全／效果證據，也不代表所有熱門商品都差 |
| 熟悉品牌與重複選擇：[Macdonald、Sharp，JBR，2000，DOI 10.1016/S0148-2963(98)00070-8](https://byronsharp.wordpress.com/wp-content/uploads/2008/04/4033.pdf) | 澳洲 462 人飲料選擇／試飲、五次選擇；品牌認知可成為持續的選擇捷徑 | 熟悉、可信與實際品質是不同概念；短期重複試驗不是保養品長期回購 |
| NEW 標籤：[Bagatini 等，REAd，2024，DOI 10.1590/1413-2311.397.117742](https://www.scielo.br/j/read/a/C8b5MmBHcPTnDwNG9KxXPjN/?lang=en) | 195 位美國受試者、價格標籤 × NEW 標籤的隨機實驗，結果是旅行杯購買意願 | 是標籤呈現交互作用，不是新配方功效、實際成交或回購證據 |
| 台灣消費情境：[MIC 2025 直播電商調查原始發布](https://mic.iii.org.tw/news.aspx?List=12&id=714) | 2024/11–12 網路調查、1,068 份；有網購經驗者中 73% 曾看、31% 曾買，美妝為偏好下單品類第二 | 31% 不是觀看者轉換率；回溯自述不能分離 KOL、折扣、平台效果，也不是全台民眾比例 |

上述研究用於選擇待驗證特徵及實驗問題，不把相關係數、SEM 路徑、意願尺度或其他產業收益搬成美妝排名權重。台灣語言、通路、膚況與品類差異，必須以本站可追溯資料及受控驗證處理。

### ChatGPT／Gemini 官方機制的可借鏡部分

2026-11-17 核對 [OpenAI《Shopping with ChatGPT Search》](https://help.openai.com/en/articles/11128490-shopping-with-chatgpt-search)：官方說明會考慮問題、偏好、預算、Memory／Custom instructions，以及商品結構資料；使用者可要求調整結果，商品結果與廣告分開。[ChatGPT Memory 官方說明](https://help.openai.com/en/articles/8590148-memory-faq)另列查看、修改及移除記憶的控制。這支持「偏好可修正、來源可追溯」的設計方向，並未公開可移植到本站的推薦公式。

[Google《Get personalization in Gemini Apps》](https://support.google.com/gemini/answer/16598623?hl=en)說明個人化可取自過去對話、使用者指示及所連結應用程式；[Gemini Privacy Hub](https://support.google.com/gemini/answer/13594961?hl=en)說明記憶／歷史及資料控制。功能依帳號與資格而異，不能假設學校帳號或本站已具同等存取。這些是官方功能說明，**不是獨立準確率、轉換效果或與本站的勝負證據**；本站亦不因此自動取得 Google／ChatGPT 個資。

### 後續優先清單與驗收

| 優先序 | 本輪範圍／後續工作 | 驗收條件 |
|---|---|---|
| **P0：本次修復** | 對齊會員偏好與當輪需求、personal-v3；讓 catalog／指定品牌／網路候選共用過敏、避用、預算及品類限制；保留成分／來源 metadata、未知評價與推薦限制；檢查價格估算與幣別 | 工作區修補須經同版後端／前端契約、不同偏好換序及硬限制不可補分的負測，再以 canonical 部署驗證。卡片呈現實際理由／缺件，不顯示未校準的適合機率；本節不宣稱正式已完成 |
| **P1：使用者能改偏好及回饋** | 提供查看／修正／撤回喜歡與避用品牌、質地、預算及目標；區分收藏／點擊、買過、實際用過、滿意與不適回饋；當輪更正優先於舊資料 | 同會員跨輪／跨 worker 一致、不同會員隔離；刪改會影響後續推薦；一次點擊不能變成人格或真實使用結論，私人資料不進公開日誌 |
| **P2：商品證據補齊** | 分開 variant、容量、地區／年份配方及 INCI；保留價格觀察日、來源、幣別、換算限制；評論有樣本數、來源、觀察時間與贊助狀態，避免重複聚合 | 每個關鍵主張可回來源，過期／未知明示；主打成分與完整配方不混用，不以同品牌其他版本代替。只有可核對資料才入相關分數 |
| **P3：有曝光紀錄的受控 A/B** | 在相同合格候選與硬限制下比較現行／個人化／受限探索；記錄政策版本、實際權重、曝光位置、候選與分派，事先固定主要指標及停止規則 | 區分點擊、收藏、可核實成交、使用後適配滿意度、不適／退換與回購；**CTR 不是唯一目標**。報樣本、信賴區間及分群缺口，避免熱門曝光回饋迴圈；沒有訂單證據就不稱成交改善 |

P1–P3 是待執行清單；本輪來源研究、離線測試或少量網站 smoke 均不能替代這些驗收，也未授權因此啟用新資料連接、公開個人資料或調低既有資安／模型品質門檻。

依本輪覆蓋率調整實作順序：先執行 P2 的小批商品資料補齊，與 P1 的偏好確認並行；有可追溯曝光與使用回饋後，才進入 P3 權重校準。第一批以現有可核對完整配方的商品為起點，逐一核對同地區、同容量／配方版本、現價、價格觀察日與商品質地標示；不能用 LLM 猜值補滿欄位。

### 本輪驗收與目前可用狀態

前次個人化網站驗收使用 `.next-personal-20261117`，未重啟學校模型。兩個真實網站登入流程的臨時帳號，使用相同「推薦保濕乳液」問題、分別保存清爽／滋潤偏好，皆回傳 200、三款商品及 `personal-v3` 證據，第一名不同，且每個第一名含對應質地理由。完整回覆各為 **13.3656 秒、9.8255 秒**；只有兩次功能樣本，不是壓測、P95、使用滿意度或膚況適合準確率。所有臨時帳號已刪除，未讀真實會員資料；完整結果在 `.runtime/reports/recommendation-personalization-20261117/website-personalization.json`。

網站測試亦抓到正式網域限定的 Origin allowlist 會阻擋 Local 3000 的聊天。已在唯一 canonical `.env` 加入 `http://127.0.0.1:3000`、`http://localhost:3000` 兩個明確來源，保留既有網域、不加 wildcard。登入態空訊息在兩個合法本機來源均進入正常 422 schema 驗證，非信任來源仍回 403；沒有因此放寬認證或停用安全檢查。檢查紀錄在 `origin-security.json`。

**交付前最後一次狀態檢查時，既有 SSH master 已消失，8002／8025 的模型通道中斷。** 本機前後端、前端建置對齊與 Redis 仍運行，backend `/readyz` 與網站照片分析因遠端模型連線不可用而未就緒。唯讀確認沒有其他既有 master/socket 可接手；未啟動新登入、未輸入或保存密碼，遠端模型及研究程序目前未重新驗證。因此上面的兩次成功測試是已留存的功能證據，不能宣稱交付當下 GPU API 仍可用；需使用者恢復 SSH 認證通道後再查 canonical status。這不影響本次研究、程式修補與離線驗證已完成的範圍。

### 2026-11-17 商品證據防線與工作區精簡

商品 catalog、Neo4j 匯入及五條推薦查詢現在完整攜帶 `texture`、`skin_types`、各自來源 URL 與驗證狀態。個人化排序只接受安全 HTTP(S) 來源及 `verified`、官方商品頁或授權零售商等人工核對狀態；商品名稱、描述、機器推定、含帳密 URL 及舊 `suitable_skin_types` 都不能冒充可核對證據或取得質地／膚質加分。唯讀覆蓋率為 8,799 筆可推薦商品、19 筆完整 INCI、5 筆可追溯台幣價格，兩者同時具備只有 2 筆；已驗證結構化質地與膚質仍為 0，故本輪沒有為了補滿欄位寫入資料庫。

首批來源查核顯示，[妮維雅台灣安溯齡肌活精華官方頁](https://www.nivea.com.tw/products/%E5%A6%AE%E7%B6%AD%E9%9B%85%E5%AE%89%E6%BA%AF%E9%BD%A1%E2%84%A2%E8%82%8C%E6%B4%BB%E7%B2%BE%E8%8F%AF30ml-40060001813180143.html)可支持 30 ml 與完整 INCI，[屈臣氏商品頁](https://www.watsons.com.tw/nivea-%E5%A6%AE%E7%B6%AD%E9%9B%85-%E5%A6%AE%E7%B6%AD%E9%9B%85%E5%AE%89%E6%BA%AF%E9%BD%A1%E8%82%8C%E6%B4%BB%E7%B2%BE%E8%8F%AF30ml/p/BP_329057)可作具觀察時間的 NT$999 價格來源，但沒有足以建立質地或適用膚質的明示資料。[巴黎萊雅台灣玻尿酸瞬效保濕水光精華官方頁](https://www.lorealparis.com.tw/rv-filler/rv-ha-serum-30ml)支持 30 ml、完整 INCI、敏感肌適用及「透明液體質地」；官方目前價格為 NT$790，舊 NT$649 不再當現價。透明液體也不自動映射為清爽／輕盈。以上是待審核匯入證據，不代表已寫入正式圖譜。

工作區只保留正式 `.next-final-20261117`。十個舊／候選前端建置、兩個中斷下載、兩個 `.partial` checkpoint、可重建 cache、`.DS_Store` 及未再引用的 7.2 MB GenSen 字型已移至 macOS 垃圾桶，合計約 1.9 GB，可復原且尚未清空；完整 checkpoint、訓練資料、校驗紀錄與復原手冊均保留。過時的一體式 `RESTORE_README.md` 已由現行容器復原手冊取代，796 行舊平台入口已精簡成 Mac／MI300X 操作索引。Markdown 原始文件總量本來僅約 0.41 MB，未把刪文件誤稱為網站效能改善。

前端不再全站下載 GenSen 字型；TypeScript 冷測由 4.31 秒降至 2.76 秒（約 36%，後續暖機值不作比較），VS Code 不再監看 `.runtime`、`.next*`、venv 與 cache，候選 build 也不再污染 `tsconfig.json`／`next-env.d.ts`。最終 build ID 為 `3svlZKWZnR73mvrjAyTet`；首頁、聊天、商品、照片頁及 runtime smoke 均為 200，舊字型路徑為 404。後端完整離線回歸 **931 passed、17 skipped、10 subtests passed**；前端 contracts、TypeScript、正式 build 與 runtime shell 語法通過。

最終前端已切換，後端亦在完整回歸後以既有 2 workers 設定重啟。使用者恢復 SSH master 後，已核對實際主機為 MI300X、`/mnt/goodagents` 為可寫 NFS，並在不重啟模型的情況下補回 8002／8025 loopback forwards；Mac 網站、build 對齊、backend `/readyz`、Redis、embedding 與網站照片健康狀態全為 UP。BGE 實際回傳 1 組 1024 維向量；照片模型 sample 實際推論 200、5.70 秒，回 `needs_confirmation`、2 項觀察與 1 項限制，仍要求使用者確認。

GraphRAG 以獨立唯讀驗收開啟 flags 後，精確別名「玻尿酸」可回 3 節點／2 關係；同程序暖查約 0.53 秒。正式 `.env` 的 knowledge graph／query flags 仍為 0，未直接對外啟用；語意錯字路徑會因 `reranker_not_configured` 拒絕，而不是降級成未複核結果。NFS 已有 manifest 指定 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` 的 reranker cache 與權重 blob，但缺正式 model path、獨立已驗證 `RERANKER_ENV` 與 8006 服務，故本輪不下載、不共用 BGE env、不宣稱完整 GraphRAG 已上線。Graph 檢索本身使用 Neo4j／BGE／可選 reranker；vLLM 不是目前 graph retrieval 的必要執行層，Terra 只在開啟解說時使用。

## 10. 2026-11-17 可撤回推薦偏好垂直切片

本輪沒有再調 `personal-v3` 權重，也沒有重寫 Product Agent。新增的維護邊界固定為：

```text
Profile RecommendationPreferencesCard
  → authenticated /api/user/preferences
  → PostgreSQL RecommendationPreferenceRepository
  → UserAnalysisContextBridge（每輪讀取）
  → EligibilityPolicy
  → EvidenceScoringPolicy personal-v3
  → bounded presenter
```

`GET/PATCH/DELETE /api/user/preferences` 只接受登入 session 的 owner，不接受瀏覽器傳入 user ID。沿用既有 `user_preferences`，沒有 migration；PATCH 區分省略（不變）、`null`／`[]`（撤回）及有值（取代），以 user row lock 序列化同會員的兩 worker 更新。此功能只管理單品預算上限、清爽／滋潤質地、喜歡品牌與避用品牌；全部撤回不會刪除禁忌成分、其他偏好或會員資料。品牌輸入正規化、去重，喜歡／避用不得重疊；前端與後端共同限制預算 1–100,000、喜歡品牌最多 5、避用品牌最多 10、單一品牌最多 40 字。

Profile 只掛一個獨立偏好卡與一個小型 API client，沒有把表單邏輯繼續塞進大型 page／通用 API 檔。卡片具 loading、dirty、saving、success、error、retry、單項移除及二次確認的全部撤回；站內導航與關閉分頁都會提醒未儲存內容。質地使用 `fieldset/legend`、toggle 有 `aria-pressed`、brand chip 有明確移除標籤、狀態與錯誤分別用 `role=status`／`role=alert`。既有膚質仍屬 Profile 單一真相，只補敏感肌與未設定，不複製到偏好表。

Bridge 改由 `users` 起始 LEFT JOIN，會員尚未建立 `user_profiles` row 仍可讀偏好；明確空清單不再落回 legacy texture key。顯示層亦移除「從商品名稱猜水感／清爽／輕盈」的 fallback；沒有 scorer／evidence 理由時，明示資料不足，不把名稱當商品標示證據。保存值在下一輪由既有每輪 refresh 讀取，因此跨 worker 生效；當輪明確預算／質地仍可暫時優先，但不會反寫長期偏好。

驗證為後端完整 **938 passed、17 skipped、10 subtests passed**，前端 contracts、TypeScript 與 production build 通過。正式前端已切換為 `.next-preferences-20261117`，build ID `9vfA9_NgHuKjn4EbzKcas`；後端依 canonical runtime 重啟為 2 workers。匿名呼叫偏好 API 回 401，OpenAPI 只暴露 GET/PATCH/DELETE；網站、build alignment、backend `/readyz`、Redis、embedding 與照片分析均為 UP。

下一個切片不是再加欄位或調權重，而是把商品 review state 從 process-local JSON 移到 Postgres，先建立兩筆 `ProductEvidenceDraft` 的 staging → review → activate 模板。必須同時補 variant key、價格／容量／評論 fingerprint、revision conflict 與 Neo4j 冪等投影；在此之前不批量爬 8,799 筆，也不把未驗證質地或評論送入分數。之後再拆 `UserAnalysisContextBridge.load()` 與 snapshot 寫入，消除每輪重複 COUNT／upsert，最後才建立 click／買過／用過／滿意／不適的分離事件與 A/B calibration。
