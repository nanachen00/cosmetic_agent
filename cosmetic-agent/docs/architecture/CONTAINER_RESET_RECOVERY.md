# 學校 GPU 容器重置後的復原手冊

基準日期：2026-11-16（Asia/Taipei）。適用架構：**Mac 執行前後端，學校 MI300X 執行模型訓練與推論，模型 API 透過 SSH 通道呼叫**。

這份手冊提供復原程序，並不會觸發重置。截至此基準日期，已驗證模型服務程序重啟、檔案校驗與代表性 checkpoint 還原；**真正的容器重建尚未執行**。學校管理介面的重建權限、容器映像 ID 與 PVC 規格仍需管理者確認。

## 1. 先判斷是哪一種狀況

| 狀況 | 處理方式 |
|---|---|
| SSH 斷線，學校容器仍在 | 重接 SSH、檢查模型 API；不需要重建容器 |
| 只重啟 Jupyter kernel 或模型程序 | 使用受管啟動／狀態腳本，標記為程序恢復；不當成容器重建 |
| 容器已被替換，原 NFS 保留 | 依本手冊第 3–7 節復原 |
| `/mnt/goodagents` 未掛載、來源不符或資料缺失 | 停止操作，依第 8 節交由管理者處理 |
| Mac 磁碟也被清除 | 另需 Mac 專案、`.env`、PostgreSQL／Redis 等備份；模型 checkpoint 不能還原網站帳號與對話資料 |

**重建前應由管理者確認保留原持久卷，不刪除或格式化 PVC。** 執行中的訓練先保存必要狀態並安全結束；本手冊沒有 `kubectl delete`、刪卷或終止 PID 1 的操作。

若基準日期後新增訓練、部署或模型版本，重置前須更新 checkpoint 備份、獨立 catalog 與新的 `pre` 快照。不要覆寫本次基線，也不要把未納入清單的新資料算作已備份。

## 2. 已保存的復原資料

### 架構與檔案位置

| 項目 | 位置／設定 |
|---|---|
| Mac 專案 | `/Users/qiao/Documents/cosmetic_agent` |
| 唯一 Backend／Auth 設定 | `cosmetic-agent/.env`，只保留在可信位置，權限 `600` |
| 學校持久掛載 | `/mnt/goodagents` |
| 已確認 NFS source | `192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11` |
| 模型／HF cache／checkpoint／輸出入口 | `/mnt/goodagents/models`、`hf-cache`、`checkpoints`、`outputs`；部分入口是指向同一 NFS 內既有資料的 symlink |
| 正式模型 API | BGE `127.0.0.1:8002`；Qwen 照片 `127.0.0.1:8025` |
| Mac 網站／後端／Redis | `3000`／`8010`／`6389`；後端 2 workers |
| 文字模型 | Terra；`ENABLE_TAIDE=0`、`CONVERSATION_TITLE_LLM=0` |

### 獨立保存的檔案基線

- NFS：`/mnt/goodagents/outputs/rebuild-verification/20261116-final-security-v6/pre.json`
- Mac：專案內 `.runtime/reports/security-recovery-20261116/final-rebuild-pre-v6.json`
- SHA-256：`0ea970f047ea10bf6db049890e1a6a098ecc4f0340d71a6919f19685f78bd892`
- 涵蓋 **3,046 個檔案、41,386,019,602 bytes**，包括模型、17 組 checkpoint 原件與 NFS 備份、來源、公開配置及 locks。這是重要資產基線，不是整個 NFS 的完整磁碟備份；secret `.env` 不在其中。

Mac 的 17 組獨立 checkpoint 備份在 `.runtime/checkpoint-backups/20261115` 與 `20261116`。操作後重新校驗 **147 檔、1,486,474,493 bytes**，均與獨立 catalog 一致。

Catalog：`.runtime/reports/security-recovery-20261116/mac-checkpoint-catalog.json`，SHA-256：`d21175a64422f9629f0058619e57c7b2b4f74bbf61c6c26211497e73dc61d499`。其中每筆 `Mac_source` 與 `file_sha256` 指定實際備份目錄與逐檔校驗值。

這些備份包含 LoRA 權重、配置、tokenizer 與訓練來源記錄；目前 catalog **沒有 optimizer／scheduler／RNG 檔案**。能恢復已保存的 adapter，不保證從訓練中斷的同一步精確續跑。所有研究 adapter 仍須通過品質門檻，還原不等於可以部署。

## 3. 在 Mac 確認基線並重接 SSH

先開兩個 Terminal 分頁：**A 用於 SSH，登入後是 GPU shell；B 保持在 Mac，處理網站與本機驗收。** 若另有既存 `ssh -N` 轉發分頁，保持它開著，另外準備 GPU 操作分頁 A。每段命令均標明執行位置。

### Mac 分頁 B：確認獨立基線

```bash
# Mac
(
set -e
cd /Users/qiao/Documents/cosmetic_agent
shasum -a 256 .runtime/reports/security-recovery-20261116/final-rebuild-pre-v6.json
)
```

結果必須等於第 2 節的 SHA。若 Mac 副本缺失或 hash 不符，先找回可信備份，不改用 NFS 上檔案自己計算的新 hash 冒充原基線。

### Mac 分頁 A：建立 SSH 與模型通道

先檢查既有通道；若已正常，沿用轉發。若連接埠被未知程序占用，先查明，不強制終止。

```bash
# Mac
lsof -nP -iTCP:8002 -iTCP:8025 -sTCP:LISTEN
```

**既有 `ssh -N` 通道的空白畫面不是 GPU shell。** 沿用它時，另外在新分頁 A 登入，不重複綁定 `-L` 埠：

```bash
# Mac 新分頁 A；登入後才可貼上後面的 GPU 命令
ssh -4 -S /tmp/blink-mi300x-control -p 30892 \
  -o StrictHostKeyChecking=yes root@140.119.24.206
```

若既有通道沒有 control socket，SSH 會另行要求認證。確認出現遠端 `root@…` 提示符；分頁 B 仍留在 Mac。若已用此方式沿用通道，跳過下面的新建通道命令。

以下是已驗證的 SSH 入口；**重建後若學校變更 host／port，先向管理者取得新值，不從 Jupyter `/lab` 網址推測。** 舊通道已斷線且兩個埠可用時，執行：

```bash
# Mac 分頁 A；登入成功後此分頁變成 GPU shell
ssh -M -S /tmp/blink-mi300x-control -4 -p 30892 \
  -o ControlPersist=8h \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -L 127.0.0.1:8002:127.0.0.1:8002 \
  -L 127.0.0.1:8025:127.0.0.1:8025 \
  root@140.119.24.206
```

密碼只輸入 Terminal，輸入時不顯示字元是正常的。這裡刻意保留互動 shell，成功後應看到遠端 `root@…` 提示符；保持分頁 A 連線。正式流程不需要 TAIDE `8000` 或候選服務 `8027`。

**SSH host key 若改變，立即停止登入嘗試。** 由學校 console／管理者核對目前主機的所有 SSH 公開指紋後，再更新本機信任資料。不可使用 `StrictHostKeyChecking=no`，也不可把刪除 `known_hosts` 當成身分驗證。

## 4. 在 GPU 確認掛載與重建後資產

### GPU 分頁 A：先驗掛載，不建立資料夾

```bash
# GPU
/usr/bin/python3 - <<'PY'
import json, subprocess
expected = '192.168.57.211:/AMD_vol/pvc-fa442967-d832-4eaa-a341-8d46317d8c11'
mount = json.loads(subprocess.check_output(
    ['findmnt', '-J', '-T', '/mnt/goodagents'], text=True
))['filesystems'][0]
valid = (mount['target'] == '/mnt/goodagents'
         and mount['source'] == expected
         and mount['fstype'] in {'nfs', 'nfs4'}
         and 'rw' in mount.get('options', '').split(','))
if not valid:
    raise SystemExit('STOP: 持久掛載不符，請找學校管理者；不要 mkdir 或下載模型。')
print('OK: 預期的可寫 NFS 已掛載')
PY
```

任何失敗都先停下。不要執行 `mkdir -p /mnt/goodagents` 來消除錯誤；普通資料夾不能代替持久掛載。

### GPU 分頁 A：驗證基線與工具本身

使用映像內的系統 Python 讀取已核對的獨立基線，先確認驗證工具與操作配方沒有改變，再執行它們：

```bash
# GPU
/usr/bin/python3 - <<'PY'
from pathlib import Path
import hashlib, json
root = Path('/mnt/goodagents')
pre = root / 'outputs/rebuild-verification/20261116-final-security-v6/pre.json'
wanted = '0ea970f047ea10bf6db049890e1a6a098ecc4f0340d71a6919f19685f78bd892'
if hashlib.sha256(pre.read_bytes()).hexdigest() != wanted:
    raise SystemExit('STOP: pre.json 與獨立 Mac 基線不符')
baseline = json.loads(pre.read_text())
verifier = root / 'blink-runtime/src/container-recovery-20261116-v6/verify-container-rebuild.py'
if hashlib.sha256(verifier.read_bytes()).hexdigest() != baseline['verifier_sha256']:
    raise SystemExit('STOP: 復原驗證工具校驗不符')
for relative in [
    'blink-runtime/src/mi300x-operator-20261116-v2/commands.json',
    'blink-runtime/src/mi300x-ops-20261116-v3/ops/runtime/source-manifest.json',
]:
    path = root / relative
    expected = baseline['critical_files'][str(path)]['sha256']
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit('STOP: 操作配置校驗不符')
recipe = root / 'blink-runtime/src/mi300x-operator-20261116-v2/commands.json'
commands = json.loads(recipe.read_text())
ops = Path(commands['source'])
manifest_path = ops / 'source-manifest.json'
if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != commands['source_manifest_sha256']:
    raise SystemExit('STOP: 啟動來源清單不符')
for name, expected in json.loads(manifest_path.read_text()).items():
    if hashlib.sha256((ops / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('STOP: 啟動來源檔案校驗不符')
print('OK: 基線、工具、操作配置與啟動來源均已校驗')
PY
```

檔案缺失同樣停止，不用重新產生 hash 來放寬驗證。來源與權重仍在 NFS 上時，不需要重新下載模型。

### GPU 分頁 A：執行真正的重建後檢查

先執行已驗證的硬體／掛載檢查，再執行 `post`。這時先不啟動訓練或正式模型，因為 `post` 本身會暫時載入官方 Qwen base 到 GPU。

```bash
# GPU
(
set -e
bash /mnt/goodagents/blink-runtime/src/mi300x-ops-20261116-v3/ops/runtime/verify-mi300x-host.sh

/usr/bin/python3 \
  /mnt/goodagents/blink-runtime/src/container-recovery-20261116-v6/verify-container-rebuild.py \
  post \
  --snapshot /mnt/goodagents/outputs/rebuild-verification/20261116-final-security-v6/pre.json \
  --snapshot-sha256 0ea970f047ea10bf6db049890e1a6a098ecc4f0340d71a6919f19685f78bd892
)
```

**預期成功條件：** `status=container_and_persistent_assets_verified`、`actual_rebuild_verified=true`、`failures=[]`。工具核對新容器身分、原 NFS／sentinel、3,046 檔的 hash、模型環境版本與官方 Qwen GPU 載入；輸出 `post-時間.json` 至同一驗證目錄。

如果只重啟了 kernel／程序而容器 ID 沒變，工具會拒絕把它算成容器重建。保留該結果，回到程序恢復流程；不要改判定讓它通過。

若已由學校另行確認 SSH host key 確實更換，`post` 還需 `--host-key-evidence <可信確認紀錄.json>`。格式為：

```json
{
  "method": "school_console",
  "verified_at": "由管理者填入含時區的確認時間",
  "fingerprints": {
    "ssh_host_ed25519_key.pub": "SHA256:由管理者另行確認的值",
    "ssh_host_ecdsa_key.pub": "SHA256:由管理者另行確認的值",
    "ssh_host_rsa_key.pub": "SHA256:由管理者另行確認的值"
  }
}
```

`method` 可為 `school_console` 或 `school_administrator`；`fingerprints` 必須與當前全部公開 host keys 相符，鍵名以實際存在的檔案為準。此範例不能原樣當作確認紀錄。管理者可從可信 console 執行驗證器的 `identity` 子命令取得公開指紋；若 NFS 工具不可用，在學校可信 console 逐檔讀取：

```bash
# 學校可信 console；由管理者執行
for recovery_public_key in /etc/ssh/ssh_host_*_key.pub; do
  ssh-keygen -lf "$recovery_public_key"
done
```

紀錄中不放私鑰、token 或密碼。透過尚未信任的 SSH 連線讀到的指紋，不能自己充當外部確認證據。

## 5. 在 GPU 啟動正式模型

`post` 通過後，使用已封存的操作配方。它啟動 BGE 與照片服務，停用 TAIDE／reranker；不執行模型下載或套件安裝。

```bash
# GPU
/usr/bin/python3 - <<'PY'
from pathlib import Path
import hashlib, json, subprocess
root = Path('/mnt/goodagents')
pre = root / 'outputs/rebuild-verification/20261116-final-security-v6/pre.json'
if hashlib.sha256(pre.read_bytes()).hexdigest() != '0ea970f047ea10bf6db049890e1a6a098ecc4f0340d71a6919f19685f78bd892':
    raise SystemExit('STOP: 基線不符')
baseline = json.loads(pre.read_text())
recipe = root / 'blink-runtime/src/mi300x-operator-20261116-v2/commands.json'
if hashlib.sha256(recipe.read_bytes()).hexdigest() != baseline['critical_files'][str(recipe)]['sha256']:
    raise SystemExit('STOP: 操作配方不符')
commands = json.loads(recipe.read_text())
ops = Path(commands['source'])
manifest_path = ops / 'source-manifest.json'
if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != commands['source_manifest_sha256']:
    raise SystemExit('STOP: 啟動來源清單不符')
for name, expected in json.loads(manifest_path.read_text()).items():
    if hashlib.sha256((ops / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('STOP: 啟動來源檔案校驗不符')
subprocess.run(commands['start_argv'], check=True)
subprocess.run(commands['status_argv'], check=True)
PY
```

預期只有正式 `8002`、`8025` 在學校 loopback 就緒。BGE v3 必須先完成 GPU 暖機與 1024 維向量檢查才 ready。沿用 `.active.json` 的已驗證 source／Python；不要直接 `uvicorn`、`nohup`，或從舊 PID 檔執行 `kill`。

控制程序使用 `/usr/bin/python3`，必須實際支援 `pidfd_open`／`pidfd_send_signal`。本次實測 Conda 與持久化模型 Python 沒有這兩個介面，不能只看 Python 版本較新就替換控制 interpreter。模型仍使用 NFS 上獨立 Python 3.11、Torch 2.13.0+ROCm7.1 環境。

## 6. 在 Mac 驗證網站與真正推論

### Mac 分頁 B：先看現有服務

```bash
# Mac
(
set -e
cd /Users/qiao/Documents/cosmetic_agent
BLINK_RUNTIME_ROLE=mac ./ops/runtime/status-production.sh
curl -fsS --max-time 10 http://127.0.0.1:8010/readyz
curl -fsS --max-time 5 http://127.0.0.1:3000/api/skin-photo
)
```

Mac 沒重置且以上正常時，直接做推論驗收，不必重建前端或初始化資料庫。若 Mac 服務確實需要重新啟動，使用受管腳本；身分不符或未知埠占用時停止排查：

```bash
# Mac；只有需要重新啟動時執行
(
set -e
cd /Users/qiao/Documents/cosmetic_agent
BLINK_RUNTIME_ROLE=mac ./ops/runtime/stop-production.sh
BLINK_RUNTIME_ROLE=mac ./ops/runtime/start-production.sh
)
```

沿用唯一的 `cosmetic-agent/.env`；維持 2 workers、Redis 必要旗標、`ENABLE_TAIDE=0`、`CONVERSATION_TITLE_LLM=0` 與已驗證前端 build。不要用 GPU 上的歷史 `.env` 覆蓋 Mac 設定，也不要在服務運行中重建其正在使用的 Next 輸出目錄。

### Mac 分頁 B：使用既有 embedding client 驗證

```bash
# Mac
(
set -e
cd /Users/qiao/Documents/cosmetic_agent/cosmetic-agent
.venv/bin/python - <<'PY'
from dotenv import load_dotenv
from app.clients.embedding_client import EmbeddingClient
import math
load_dotenv('.env')
client = EmbeddingClient.from_env()
vector = client.embed_query('連線驗證')
valid = (len(vector) == 1024
         and all(math.isfinite(x) for x in vector)
         and abs(math.sqrt(sum(x*x for x in vector)) - 1) < 1e-5)
if not valid:
    raise SystemExit('STOP: embedding 內容不符')
print({'embedding_ok': True, 'dimension': len(vector)})
PY
)
```

沿用既有 timeout／重試設定。失敗就記錄錯誤類型；不要用放寬 timeout 或反覆重試替代冷啟動驗收。

### Mac 分頁 B：驗證照片 GPU 推論

以下只使用伺服器內的公開樣本，不上傳私人照片：

```bash
# Mac
curl -fsS --max-time 90 \
  -H 'Content-Type: application/json' \
  --data '{"sample":true}' \
  http://127.0.0.1:8025/analyze
```

接著用既有帳號登入網站，以公開樣本完成一次會員照片分析。確認需要使用者確認、不自動存檔；不要把密碼、cookie、API key 或私人照片寫進報告。

GraphRAG 另做既有 Neo4j／SkinConcern 唯讀檢索：使用固定公開查詢與 BGE 模型名 `bge-m3`，記錄連線與結果筆數，不修改索引或資料。原基線為 52 筆 SkinConcern、一次實際向量檢索回 1 筆；未來資料變動須另行記錄。這只證明底層讀取鏈，完整 LLM 引用回答、SMTP／OAuth、手機準確率與長時間壓測另列驗收。

## 7. 完成條件與保留紀錄

| 驗收項目 | 通過條件 |
|---|---|
| 容器／儲存 | 新容器身分、正確 NFS／sentinel，`post` 無 failures |
| 重要資產 | 基線檔案 hash 一致；模型、17 組原件與 NFS 備份均可讀 |
| 模型 | BGE／Qwen ready，真實向量與公開照片推論成功 |
| Mac | 網站／後端／照片 proxy 正常，2 workers、Redis 就緒 |
| 網站會員路徑 | 登入、照片分析、確認後才存的流程正常 |
| GraphRAG 讀取 | 實際只讀向量檢索成功；沒有用 health 取代查詢 |

任一項失敗，記為「部分恢復／尚未通過」，保留原始錯誤；不要改 pre、hash、模型名稱或關閉安全檢查讓結果變綠。

將新 `post-時間.json`、服務 status、推論結果摘要及排錯紀錄另外保存至 Mac；原 `pre.json` 與其獨立 SHA 也保留。驗收通過後，才依新的穩定狀態建立下一份有新 run ID 的基線。

## 8. 失敗時怎麼處理

| 問題 | 處理方式 |
|---|---|
| SSH host／port 改變或拒絕連線 | 向學校確認新的 SSH 入口與服務狀態；不猜 Jupyter port |
| host key 不符 | 走學校 console／管理者的外部指紋確認，再處理信任紀錄 |
| NFS 不在、來源不符、唯讀或權限不足 | 停止，請管理者恢復正確持久掛載；不建立空目錄、不格式化或替換原卷 |
| `pre`、工具、模型或 source hash 不符 | 保留現有檔案與錯誤，確認是否用了過期基線或資料損壞；不覆蓋原件、不重算 hash 冒充通過 |
| ROCm／Python／控制介面缺失 | 管理者確認相容映像與 GPU 暴露；使用已記錄 locks／來源修復，不原地升降舊 vLLM、不安裝 NVIDIA Triton 代替 ROCm |
| 重建後模型埠被占用／PID 身分不符 | 先用受管 status 查新身分；保留舊 PID 檔，不盲目刪檔或送出數值 PID signal |
| BGE health 正常但首個查詢 timeout | 核對實際 active 是 v3 與暖機流程；保存底層原因與時間，不用延長 client timeout 掩蓋 |
| Neo4j、SMTP、OAuth 或其他外部服務缺設定／失聯 | 模型服務可分項標示恢復，對應功能列為未完成；不假稱全平台已復原 |

### 部分 checkpoint 或整個 NFS 資料遺失

先由管理者確認原卷能否從儲存快照恢復。原 catalog 的 Mac checkpoint 備份只涵蓋已保存的 17 組 adapter；本輪 cycle4 另由下述 watcher 逐檔記錄。兩者都不涵蓋整個 volume、所有原始訓練照片或網站資料庫。

若需要從 Mac 取回某組 checkpoint：

1. 依獨立 catalog 的 `Mac_source`／`file_sha256` 選定該組，先在 Mac 逐檔核對。
2. 確認正確 NFS 已恢復且可寫後，以 SCP 複製到新的 `/mnt/goodagents/outputs/rebuild-verification/restore-staging/<新標籤>`，保留現有原件。
3. 在 Mac 將該組 catalog 的 `file_sha256` 原樣另存為 `checkpoint-sha256.json`、計算本地 SHA，再把同一份清單複製到上述 staging 內。
4. 使用既有 v6 工具的 `restore-check --directory <新 staging> --manifest-sha256 <Mac 清單 SHA> --expected-source <已確認 NFS source>` 驗證，再由維運人員決定正式放回位置。
5. 只有 staging 驗證通過，不代表所有 checkpoint、環境或整個容器已恢復；也不授權把 adapter 部署到網站。

若 NFS 的復原工具也遺失，可從 Mac 的 `.runtime/container-recovery-20261116-v6` 取回並對照獨立 pre 中的 verifier hash；模型服務來源／操作配方也各有 Mac `.runtime` 凍結副本。新卷與原 source 不符時，由管理者完成儲存對應及新的復原計畫，不能直接修改本手冊的 expected source 硬過舊基線。

官方權重缺失時，依已記錄的 repo／revision／SHA 分別處理，只補缺失且要使用的模型，先下載至隔離位置並校驗，不一次下載全部模型。尚缺的 optional TAIDE2602 與歷史 `BLINK_Passed_Research_Adapter.zip` 原本就沒有恢復，不得因此宣稱已找回；目前正式服務不需要啟動它們。

## 9. 新增研究訓練的續接（2026-11-16 cycle4）

這是原 v6 pre 建立後的新訓練，舊 3,046 檔基線不涵蓋這批新增資產，也不能證明它們已通過容器重建。請先完成前述掛載、GPU、官方權重、正式服務及 source/hash 驗證；目前沒有執行容器重建。

| 資產 | NFS 路徑（相對 `/mnt/goodagents`） |
|---|---|
| 固定資料 | `data/training/skin-appearance-clean-20261116-r1`；559 train／80 validation；test 保持封存 |
| 研究來源 | `blink-runtime/src/qwen-app-cycle4-training-20261116-v3`；內有 `source-manifest.json` |
| 獨立訓練環境 | `blink-runtime/envs/qwen-app-cycle4-secure-t213-r71`；`training-runtime-ready.json` 記錄 approved locks／45 版本與 wheel SHA |
| 輸出／協定 | `outputs/skin-vlm/qwen-app-cycle4-20261116-r3`；`protocol.json`、`status.json`、`job.json`、驗證 partial predictions |
| 續訓 checkpoint | `checkpoints/skin-appearance-decision/qwen-app-cycle4-20261116-r3/<配方>/step-XXXXXX` |
| NFS 第二份 | `checkpoint-backups/qwen-app-cycle4-20261116-r3/<配方>/step-XXXXXX`；同一 NFS，不是獨立儲存 |

Mac 新增獨立備份目錄為 `.runtime/reports/qwen-app-cycle4-20261116-r3/mac-checkpoints`，以同目錄根的 `watch-status.json` 所列 `complete: true` 及逐檔 SHA 為準；只存在 `.partial` 或正在傳輸的項目不算完成。舊 r2 pilot 另存 `.runtime/reports/qwen-app-cycle4-20261116/mac-checkpoints/qwen-app-cycle4-20261116-r2`，已驗 12 檔；它是工程 smoke，不是選出的網站模型。

使用者已選擇停止 Mac 全量備份；已完成副本及 partial 均保留，不自動重啟補拷。這不影響已完成的六組訓練與 NFS 原件／對照副本校驗，也不是沿用同一持久卷復原的阻擋項。重建後先驗掛載與 NFS 現存檔案；若整個 NFS 卷遺失，Mac 只能還原已完整校驗的部分，不能把未完成傳輸當成完整備援。

每個新 checkpoint 含 safetensors adapter、processor、optimizer／scheduler、Python／CPU／GPU RNG、epoch／資料順序與 SHA manifest。只有 `files.sha256.json` 最後提交且全檔 hash 相符的 `step-*` 可續接；不使用或覆蓋不完整目錄。Loader 固定 `weights_only=True`，不接受任意 pickle；不是把歷史僅有 adapter 的 17 組假稱為可精確續訓。

先確認原研究 job 已停止或因重建消失，再在**已確認的 GPU SSH shell** 執行原 frozen source 的明確續接；它會拒絕仍存活的同一 job、不同資料／版本／來源指紋及缺少的模型：

```bash
# GPU SSH shell — 不在 Mac 執行
TRAIN_PY=/mnt/goodagents/blink-runtime/envs/qwen-app-cycle4-secure-t213-r71/bin/python
TRAIN_SRC=/mnt/goodagents/blink-runtime/src/qwen-app-cycle4-training-20261116-v3
"$TRAIN_PY" "$TRAIN_SRC/research/launch_app_cycle4.py" \
  --python "$TRAIN_PY" \
  --data /mnt/goodagents/data/training/skin-appearance-clean-20261116-r1 \
  --model /mnt/goodagents/blink-runtime/data/models/qwen3-vl-4b-instruct \
  --output /mnt/goodagents/checkpoints/skin-appearance-decision/qwen-app-cycle4-20261116-r3 \
  --report /mnt/goodagents/outputs/skin-vlm/qwen-app-cycle4-20261116-r3 \
  --backup-root /mnt/goodagents/checkpoint-backups/qwen-app-cycle4-20261116-r3 \
  --resume
```

若 training env／來源／checkpoint 缺失，先停止並對照 approved cache／Mac 清冊還原；不要在正式照片環境安裝研究依賴，也不要改協定指紋繞過拒絕。若最新 checkpoint 未完整提交，保留該目錄供調查；不要為了重跑刪除或覆蓋它。研究程式會在照片服務忙碌時讓路，這是協作排程，不是 GPU 隔離保證。

本輪四組消融及兩 seed 複驗已完成，最高完整 JSON validation macro-F1 為 0.6304、三 seed 均未過門檻；沒有選出或部署 adapter，test 仍封存。7 組 checkpoint 原件及 6 組正式候選的第二份 NFS 備份均已逐檔驗 SHA；Mac 完整副本依使用者選擇停止補拷，實際完成數須讀 watch receipt，不能假設全部已傳完。工程 smoke 通過、loss 降低或能 resume，均不代表辨識準確率達標。

### 2026-11-17 新資料與研究批次補充

本段新增的候選、程式與研究輸出不在 11/16 的舊 v6 `pre` 基線內。實際重建前須另做新快照，不能把舊清冊當成這些新檔已驗證保存的證據；Mac 模型完整備份仍依使用者選擇停止。

- 新 MEMI 候選：`/mnt/goodagents/blink-runtime/data/training-intake/memi-ds-v1-20261116`，8 張 JPG＋8 個對應 PNG，共 10,228,751 bytes；有原作者 MD5／本地 SHA 與獨立重讀紀錄。這些不是已准入的四特徵訓練資料，也不是已完成手機外驗。
- 品質失效研究：`qwen-silver-cycle5-20261117-r1` 的 report／checkpoint／NFS 備份保留作調查。report 內 `data-quality-stop.json`，checkpoint 與備份根目錄內 `DO_NOT_DEPLOY.json` 都應保留；完整訓練停於 28/142，沒有驗證成績，**不可 resume、不可部署**。不把 pilot 可讀或梯度正常當成標註品質通過。
- 來源資料只有通過最後逐張視覺證據／SHA／四標籤一致性／品質檢查才可准入；未知值不可改成陰性。現在被檢查的 5 張弱標註皆已隔離，新的准入清單為空。不要在復原時把舊候選清單改旗標當成已核准資料。
- 後續 gold-only 研究沿用原 559 train／80 val，只做受控正例輔助權重對照；使用新 frozen source 與獨立 run ID。11/17 第一個 gold pilot 已通過，但 full resume 曾被 protocol fingerprint 差異擋下，不能把 `pilot_complete` 當成完整訓練完成。以 `job.json` 的 PID／start ticks、最新 status、實際 optimizer／validation 進度及最終比較報告共同確認。

最新資料准入、隔離與研究狀態見 Mac `.runtime/reports/agent-data-intake-20261116/intake-summary.json`；遇到品質停止或協定差異時，保留原件並找新版本／新 run 的記錄，不改 hash 繞過拒絕。這些補充沒有執行容器重建或替換網站模型。

後續 `qwen-gold-recall-cycle5-20261117-r2` 使用 `qwen-gold-recall-cycle5-training-20261117-v2`，已完成 140 步＋80 張驗證，macro-F1 0.6175、未過門檻；checkpoint 與 NFS 副本 12 檔／210,135,765 bytes 逐檔 SHA 一致，但根目錄 `NOT_SELECTED.json` 表示不能部署。結果／來源／完整性小報告保存於 `outputs/skin-vlm/qwen-gold-recall-cycle5-20261117-r2/independent-review/`。Gold r1 保留原 protocol／pilot 並加 `DO_NOT_RESUME`，full 為 0；浮點指紋問題已用固定原始指標與跨程序測試修正，沒有改寫舊指紋。上述新資產仍未因此自動加入舊 v6 重建前清冊，Mac 全量模型備份也沒有重啟。

## 10. 相關資料

- [交付狀態與完整證據說明](PLATFORM_DELIVERY_PLAN.md)
- [分離部署設定](../../../ops/runtime/SPLIT_DEPLOYMENT.md)
- Mac 最終驗證結果：`.runtime/reports/security-recovery-20261116/final-validation-summary.json`
- Mac 重建操作參數：`.runtime/reports/security-recovery-20261116/container-rebuild-operator-commands-v6.json`
- NFS 操作配方：`/mnt/goodagents/blink-runtime/src/mi300x-operator-20261116-v2/commands.json`
- NFS 最終 pre／post 目錄：`/mnt/goodagents/outputs/rebuild-verification/20261116-final-security-v6`

這些是 2026-11-16 已核對的路徑與版本。基線變更時同步更新本手冊、操作配方與獨立 Mac 備份；維持密鑰只在唯一 `.env` 或可信秘密儲存，不放進文件、Git、公開 source manifest 或驗證報告。
