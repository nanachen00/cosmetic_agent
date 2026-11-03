# P+Sys1 bundle — VM 操作說明（2026-10-26）

解壓到 `$REPO/ops/p-sys1/`（會得到 `gateB-colab/`、`fullpath-kev/`、`shadow-sys1/`、`run_vm.sh`），報告在 `docs/handoffs/REPORT_P_plus_Sys1_20261026.md`。
`gateB-colab/` 與 `fullpath-kev/` 的原始檔**沒有改**（`python3 gateB-colab/holdout.py` → `FROZEN ✓`）；新增的東西都在 `gateB-colab/sys1/`、`shadow-sys1/`、`run_vm.sh`。

## 0. 先判斷環境、跑自測（唯讀／mock）
```bash
cd $REPO/ops/p-sys1 && chmod +x run_vm.sh shadow-sys1/*.sh
./run_vm.sh where                    # python／conda env／GPU／磁碟／HF／vLLM（只讀 :8200/v1/models）／埠＋本機複現時被模擬／略過的模組（local-validation/）
cat /opt/rocm/.info/version         # 預期 6.4.x
./run_vm.sh venv                     # 建 /opt/venv-sys1：以 /opt/conda/envs/py_3.10 為基底（--system-site-packages），沿用映像的 torch 2.6.0 ROCm；缺的套件只裝進 venv
./run_vm.sh selftest                 # 全部 mock 自測；有 torch 時多跑 tiny-random-bert（torch 路徑第一次真的執行就是這裡）
```
`selftest` 任何一項失敗就停：把輸出貼回來，不要往下跑。

**2026-10-29 更正**：10/27 記錄的「VM 有 python3.11、torch 2.8.0 rocm6.4 是 Gate1 驗證組合」不適用於 10/20 重建後的容器。
- PATH 上沒有 `python3.11`；3.11 只在後端的 `/opt/conda/envs/blink-backend`。
- 重建後唯一驗證過能用 GPU 的，是映像內建 `/opt/conda/envs/py_3.10` 的 torch 2.6.0 ROCm（production embedding 也用這個環境）。
- 所以 `venv` 預設以 py_3.10 為基底建 venv，用 `--system-site-packages` 唯讀引用它，並用 constraints 鎖住 torch／numpy。缺的套件只裝進 venv，不改基底。結果寫在 `/opt/venv-sys1/sys1-venv.json`。
- 要走原設計：`VENV_MODE=wheel BASE_PY=/opt/conda/envs/blink-backend/bin/python3.11 ./run_vm.sh venv`。要下載 torch ROCm wheel（數 GB），在這個容器上沒有驗證過。
- `venv` 已存在時，不會換模式或換基底。
- 沒有 `/opt/venv-sys1` 又沒有明確設 `PY` 時，除了 `where`，其他命令都會拒絕執行，不會拿 PATH 上的 `python3`。
- Sys1 是獨立的 HTTP 服務（:8232–8239），Python 版本不必和後端（3.11）一致。所有候選在同一個 venv 裡比較，公平性不受影響。

## 1. T4 微調候選（同資料、同頭、同預算）
```bash
./run_vm.sh data                               # gateB-colab/sys1/train/data/v1（含排除檢查；寫一次）
DEVICE=cpu ./run_vm.sh train mmbert-ft         # 3 個 seed（budget.json）；DEVICE=cuda 前會檢查剩餘顯存 ≥ 24GB
./run_vm.sh train mdeberta-ft; ./run_vm.sh train xlmr-ft      # eurobert-ft 需要 trust_remote_code，先讀過它的 modeling 檔
./run_vm.sh contract mmbert-ft 13              # 只驗契約＋noul 方向＋延遲；不產生評比資料，不需簽核
```
需要能連 Hugging Face 下載基座；連不到就先在別處下載後放進 HF cache。

## 2. S2 評比（**負責人簽核來源之後**才可以）

2026-10-28 已簽核：`mmbert-ft`、`mdeberta-ft`、`xlmr-ft`（見 `gateB-colab/sys1/SIGNOFF.json`）。`gates.json`／`train/budget.json` 已定案，`train`／`s2` 前會自動執行 `sys1/signoff.py verify`，被改過就拒絕。
負責人在 `gateB-colab/sys1/registry.json` 把該候選設 `provenance_verified: true` 並填 `verified_by`/`verified_on`（研究證據在每個候選的 `evidence`／`proposed_provenance`）。然後：
```bash
./run_vm.sh s2 mmbert-ft 13      # probe → collect gate_data(all) → 10 秒倒數 → collect router_gold → analyze_cand
```
SYS1_HOLDOUT.md 會附「gate_data test 對照（非 hold-out）」：Parser only / Kev-0.8B（`sys1/reference/gateB-t4-001`，Colab 原始回答）/ 候選。`sys1/frozen/<id>.json` 只寫一次；router_gold 已被看過答案 → 結果只能用來淘汰。`router_gold_v2` 凍結後用 `run_cand.py --dataset router_gold_v2`。

## 3. Laya（T3）與 Kev 對照（2026-11-03，r7）

### 3a. Laya：`laya-mmbert`、`laya-typed`
- **來源未查證**：2026-11-03 負責人授權越過 teacher／標註來源檢查（owner override，記在 `registry.json` 與 `SIGNOFF.json` 的 `amendments`）。報告與結論都要標示「來源未查證」；要 promote 前另行決定。
- Laya 是零樣本模型，沒有訓練步驟。`laya-serve` 本身講 Kev 相同的 `POST /v1/systemone`，但沒有 `/v1/models`、推論時間只在 header、預設綁 `0.0.0.0:8000`（正式 TAIDE 閘道的埠）。所以由 `sys1/laya_entry.py` 在同一個程序裡載入它，補上這兩樣，只綁 `127.0.0.1:8231`。
- 環境：`/opt/venv-laya`，和 `/opt/venv-sys1` 一樣以 py_3.10 為基底；torch／numpy／transformers 鎖在基底版本，`laya[serve]==0.4.21`。
```bash
./run_vm.sh laya-venv
DEVICE=cuda ./run_vm.sh laya-contract laya-mmbert     # 契約＋noul 方向＋延遲 p50/p95；第一次會下載檢查點（multilingual 約 650 MB，typed-decisions 約 800 MB）
DEVICE=cuda ./run_vm.sh laya-contract laya-typed
DEVICE=cuda ./run_vm.sh laya-s2 laya-mmbert           # 同 s2：gate_data(all) → 10 秒 → router_gold → SYS1_HOLDOUT.md（runs/laya-mmbert/gold/…）
```
- 本機（Windows CPU，本機模擬）：契約兩個都通過；noul 方向 multilingual 正確（0.887 對 0.368），typed-decisions（英文檢查點）**不對**。CPU 延遲沒有參考價值（`local-validation/LOCAL_LAYA_CONTRACT_20261103.md`）。
- `laya-typed` 是英文檢查點，model card 自己建議非英文用 multilingual；預期只能當下限對照。

### 3b. Kev 在 VM 上重跑（Colab Gate B 的 P+K 部分）
- Kev 是 Qwen 基座，只當對照（policy：reference，允許收集，不得 promote）。
- Kev 1.0 程式（`jaredpalmer/kev@1f04cfa2`）要求 Python ≥ 3.12、torch 2.6–2.8、transformers ≥ 5.17，映像的 py_3.10 跑不了。`kev-venv` 用 uv 另裝 Python 3.12（`/opt/uv-python`），torch 用 ROCm 6.4 wheel（`torch==2.8.0`，數 GB；**在這個容器上沒有驗證過**），其他照 Kev 的 `uv.lock`（transformers 5.17.0、accelerate 1.15.0）。不改映像的任何環境。
- 需要連 GitHub、PyPI、download.pytorch.org、Hugging Face。
```bash
./run_vm.sh kev-venv                                  # → /opt/kev-1f04cfa2/.venv（KEV_DIR 可改）；最後確認 torch 是 ROCm 版且看得到 GPU
./run_vm.sh kev-gateb                                 # Kev :8230 → kev-collect（cal＋test＋安全補充）→ 延遲階梯 1/2/4 → 10 秒 → router_gold → 停 Kev
KEV_DTYPE=bf16 ./run_vm.sh kev-gateb runs/kev-gateb-bf16   # 預設 fp32（= Colab 與凍結門檻的精度）；bf16 是 Kev 服務的預設，較快
```
- 輸出：`runs/kev-gateb-YYYYMMDD/SUMMARY.md`（gate_data test 準確率、延遲拆解、併發階梯）與 `…-gold/SUMMARY.md`（hold-out，用凍結門檻，不調參）。可以直接和 Colab 的 `results-gateB-t4-001` 比：準確率應該接近；延遲是 MI300X 的數字。
- Gemma 臂不在這裡（需要 llama.cpp 的 HIP 版）。
- GPU 與 TAIDE（:8200）共用：在離峰時段跑。

`sys1/laya_ctx.py` 保留備用（`./run_vm.sh laya-ctx`）。

## 4. Shadow overlay（T6）→ S3（T7）→ canary／回滾（T8）
```bash
cd shadow-sys1
./find_prod_roots.sh                 # 唯讀：:8010 實際在哪個目錄跑 → 確認後填 prod_roots.json（confirmed_by/on）
python3 shadowpatch.py anchors --spec patches/sys1-dispatch-v1.json --src <現行 cosmetic-agent 根目錄>   # 唯讀，anchor 必須都 OK
python3 shadowpatch.py init-shadow --src <現行 cosmetic-agent> --dest shadow/cosmetic-agent   # r6：複本內寫死的正式路徑改指向複本（紀錄在 .shadow-copy）
python3 shadowpatch.py apply --spec patches/sys1-dispatch-v1.json --shadow-root shadow/cosmetic-agent
#   編輯 control/sys1_dispatch.json：model=<registry model_field>、frozen=<gateB-colab/sys1/frozen/<id>.json 絕對路徑>、trace=<絕對路徑>
python3 shadow_stack.py plan                                      # 無副作用：檢查 shadow root（G1/G2）、overlay、路徑改寫、上層 .env、埠、工具
python3 shadow_stack.py env-check                                 # 只列 shadow env 的鍵：已填／空白／覆寫，不印值
python3 shadow_stack.py up --ack-shared-data-services             # 獨立 DB blinkai_shadow_sys1＋Redis :6380＋backend :8210，等 /readyz，再查程式來源
python3 shadow_stack.py verify                                    # 隨時重查程式來源（報告：runs/stack/provenance.json）
python3 sys1ctl.py shadow                                         # S3：回應不變，只記錄
python3 s3_shadow.py drive --base-url http://127.0.0.1:8210 --scenarios <凍結的情境檔> --trace-text   # 訪客 session，不需帳號檔；送出前後都查來源
python3 s3_shadow.py analyze --trace <trace> --source synthetic --out runs/s3-$(date +%Y%m%d)
# 以下要負責人明確同意：
python3 sys1ctl.py canary 5 --approved-by "<姓名>"   # 5 → 25 → 100，不能跳級
python3 rollback_check.py --apply --watch --interval 15 &     # 自動回滾守門（錯誤率 5 分鐘 >2%、p95 >400ms、5xx 增加）
python3 sys1ctl.py rollback --reason "<原因>"                  # 一鍵回滾（arm=off）
python3 shadowpatch.py revert-all                               # 移除 overlay
python3 shadow_stack.py down                                    # 停 shadow backend 與 Redis（只停自己啟動的；DB 保留，--drop-db --yes 才刪）
```
請求格式已對齊 production 原始碼（analysis-source-20261013T163259Z：`ChatRequest`、訪客流程）。

**Shadow :8210 的啟動（2026-10-28 新增 `shadow_stack.py`）**：Gate2 harness 的 `bench.py` 是事故前架構（需要 LiteLLM proxy 設定、`MANIFEST.sha256`、16 個密碼會員帳號），復原後的平台三者都沒有，所以改用 `shadow_stack.py`：
- 隔離：獨立 PostgreSQL 資料庫 `blinkai_shadow_*`（schema 用 production migration 0001）、獨立 Redis :6380（密碼只在 0600 設定檔）、shadow env 由 production `.env` 加覆寫組成；SMTP、`ADMIN_USER_IDS`、Better Stack、Firecrawl、OTEL 匯出一律清空。
- 共用（需 `--ack-shared-data-services`）：Neo4j Aura（讀）、Zep Cloud（訪客記憶會寫入）、OpenAI（舊 LLM 臂）、TAIDE 閘道 :8000（共用 GPU）。
- 本機複現環境（Windows）已實際跑過兩輪（10/28、10/29）；**在 VM 上第一次 `up` 是它第一次在 Linux 上執行**，任何一步失敗都會停下並保留 log（`runs/stack/`）。
- 事故前的 `bench.py`／`auth_clients.py` 已從封存檔找回（`experiments/p-sys1/recovered/gate2-harness-20261001/`，未放進 bundle），只在要重跑舊 Gate2 時才需要。
**Shadow backend 的程式來源（2026-10-29，r6）**：團隊程式在 5 處寫死 `sys.path.insert(0, '/var/lib/jenkins/tomod/cosmetic-agent')`（`api_server.py:8`、`orchestrator.py:11` 等），skin_tracking 也寫死 `env_file='…/cosmetic-agent/.env'`。r5 以前的 shadow 複本因此會改從正式環境載入 orchestrator（overlay 不生效），也會讀正式 `.env`。r6 的處理：
- `init-shadow` 把複本內 `.py` 的這個路徑（和 `--src` 路徑）改成複本自己的路徑，改了哪些檔記在 `.shadow-copy`。r5 以前建立的複本 `plan` 會判 NG：先把它移開，再重新 `init-shadow`。
- backend 改由 `shadow_entry.py` 啟動（同一個 app、同樣的 uvicorn 設定），多一個只接受 127.0.0.1 的 `GET /_shadow/provenance`。`up` 在 `/readyz` 之後檢查：orchestrator 必須是複本裡套了 overlay 的版本，而且沒有任何模組來自正式環境、沒有打開任何 `.env`。不通過就停掉自己啟動的 backend 和 Redis（DB 保留），並拒絕繼續。
- `s3_shadow.py drive` 送出前也會檢查，不通過就不送；送完再查一次，不通過回 rc=4（這次結果無效）。
- 報告只列路徑、模組名稱與環境變數名稱，不含任何值。

`shadowpatch anchors` 已確認對 `cosmetic-agent` 全部成立；`cosmetic-agent-stable` 有 4 個錨點不同——若 :8010 跑的是 stable，需要另一份 spec。
