# BLINK AI 智慧美妝顧問

BLINK AI 是一套結合多代理系統（Multi-Agent System）、肌膚紀錄、
化妝品推薦、TAIDE 地端語言模型與使用者記憶功能的智慧美妝顧問平台。

系統提供：

- AI 美妝對話
- 問卷與個人膚質分析
- 化妝品搜尋與推薦
- 每日膚況紀錄
- 肌膚分析與追蹤
- 使用者對話記憶
- Stable／Development 雙環境
- TAIDE GPU 地端推論

---

## 全服務地圖

| Port | 服務 | 目錄 | Log | 用途 |
|---:|---|---|---|---|
| `3000` | Next.js Frontend | `frontend/` | `/tmp/frontend_3000.log` | 使用者操作介面 |
| `8010` | Stable API | `cosmetic-agent-stable/` | `/tmp/api_8010.log` | Demo／穩定版本 |
| `8011` | Development API | `cosmetic-agent/` | `/tmp/api_8011.log` | 開發與測試 |
| `8000` | TAIDE 主模型 | — | `/tmp/taide_8000.log` | 一般對話與 Agent 推論 |
| `8004` | TAIDE 膚況模型 | — | `/tmp/taide_8004.log` | 膚況分析與追蹤 |

> **重要：** 測試人員與開發者使用 Development API（Port `8011`）。
>
> Stable API（Port `8010`）是 Demo／穩定版本，只有維護者可以更新或重新啟動。

---

## 系統架構

```text
使用者瀏覽器
      │
      ▼
Next.js Frontend
Port 3000
      │
      ├──────────────────────────────┐
      ▼                              ▼
Stable API                       Development API
Port 8010                        Port 8011
cosmetic-agent-stable/           cosmetic-agent/
      │                              │
      └──────────────┬───────────────┘
                     ▼
             Root Agent Orchestrator
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
 商品推薦 Agent   問卷／Profile   膚況分析 Agent
       │             │             │
       └─────────────┼─────────────┘
                     ▼
             ┌───────┴────────┐
             ▼                ▼
       TAIDE 主模型       TAIDE 膚況模型
        Port 8000          Port 8004
```

---

## Stable 與 Development 的差別

### Development

```text
目錄：cosmetic-agent/
Port：8011
用途：開發、修改及測試
```

開發者修改後端程式後，應先在 Development 環境測試。

```bash
./team.sh dev-start
```

### Stable

```text
目錄：cosmetic-agent-stable/
Port：8010
用途：Demo 與穩定服務
```

`cosmetic-agent-stable/` 只放：

- 已完成開發的程式
- 已通過測試的程式
- 已完成 Git Commit 與 Push 的程式
- 已完成 Code Review／Merge 的程式

禁止直接在 `cosmetic-agent-stable/` 開發或測試未完成的功能。

---

## 專案目錄

```text
/var/lib/jenkins/tomod/
├── frontend/                     # Next.js 前端，Port 3000
├── cosmetic-agent/               # Dev 後端，Port 8011
├── cosmetic-agent-stable/        # Stable 後端，Port 8010
│
├── prompts/                      # GraphRAG Prompt
├── input/                        # GraphRAG 輸入
├── output/                       # GraphRAG 輸出
├── cache/                        # GraphRAG 快取
├── logs/                         # 執行紀錄
├── supervisor/                   # 程序管理設定
├── settings.yaml                 # GraphRAG 設定
│
├── team.sh                       # 團隊統一操作入口
├── start_all.sh                  # 容器重啟後啟動全部服務
├── restart_frontend.sh           # 重啟前端
├── restart_api.sh                # 重啟 Stable API
├── restart_api_dev.sh            # 重啟 Development API
├── stop_api_dev.sh               # 停止 Development API
├── restart_taide_servers.sh      # 重啟 TAIDE GPU 服務
│
├── README.md                     # 專案操作說明
└── TESTING.md                    # 功能驗收與 Bug 測試清單
```

---

## 團隊快速操作

所有操作都先進入專案根目錄：

```bash
cd /var/lib/jenkins/tomod
```

### 查看所有服務狀態

```bash
./team.sh status
```

範例：

```text
=== BLINK AI 服務狀態 ===
Frontend               port 3000  RUNNING  PID 3170140
Stable API             port 8010  RUNNING  PID 3172454
Development API        port 8011  RUNNING  PID 3175193
TAIDE Main             port 8000  RUNNING  PID 1463173
TAIDE Skin             port 8004  RUNNING  PID 1464854
```

PID 每次重啟都可能改變，這是正常現象。

Port 是固定的服務位置；PID 是目前執行該服務的程序編號。

### 執行健康檢查

```bash
./team.sh health
```

正常結果：

```text
=== BLINK AI 健康檢查 ===
Frontend               OK
Stable API             OK
Development API        OK

TAIDE 服務以 Port 狀態判斷：
TAIDE Main             port 8000  RUNNING
TAIDE Skin             port 8004  RUNNING
```

### 查看所有可用指令

```bash
./team.sh help
```

---

## Development API 操作

### 重啟 Development API

修改 `cosmetic-agent/` 內的後端程式後執行：

```bash
./team.sh dev-start
```

這個指令會：

1. 找出目前占用 Port `8011` 的舊程序。
2. 停止舊 Development API。
3. 檢查 Python 語法。
4. 啟動新的 Development API。
5. 執行健康檢查。
6. 顯示 Log 位置。

它不會操作 Stable `8010`。

### 停止 Development API

```bash
./team.sh dev-stop
```

### 查看 Development API Log

```bash
./team.sh logs dev
```

按 `Ctrl+C` 可停止持續查看 Log，不會停止 API。

---

## Frontend 操作

Frontend 使用 Next.js，監聽 Port `3000`。

### 一般前端開發

修改前端程式並儲存後，Next.js 通常會自動熱重載，不需要重新啟動。

### 重啟 Frontend

```bash
./team.sh frontend-start
```

這個指令會呼叫：

```text
restart_frontend.sh
```

並將 Log 寫入：

```text
/tmp/frontend_3000.log
```

### 查看 Frontend Log

```bash
./team.sh logs frontend
```

### 前端樣式異常或出現 404

先停止舊前端程序，再清除 Next.js Cache：

```bash
cd /var/lib/jenkins/tomod/frontend

FRONTEND_PID="$(
  netstat -tlnp 2>/dev/null \
    | grep ":3000 " \
    | grep -oP '\d+(?=/)' \
    | head -1
)"

if [ -n "$FRONTEND_PID" ]; then
  kill "$FRONTEND_PID"
fi

rm -rf .next

cd /var/lib/jenkins/tomod
./team.sh frontend-start
```

---

## Stable API 操作

Stable API 使用：

```text
目錄：/var/lib/jenkins/tomod/cosmetic-agent-stable
Port：8010
Log：/tmp/api_8010.log
```

Stable 只允許維護者更新。

### Stable 發布流程

```text
在 cosmetic-agent/ 開發
        ↓
使用 8011 測試
        ↓
完成 Bug 驗收
        ↓
Git Commit
        ↓
Push 至 GitHub
        ↓
Pull Request／Code Review
        ↓
Merge 通過
        ↓
cosmetic-agent-stable/ 執行 git pull
        ↓
restart_api.sh
        ↓
Stable 8010 健康檢查
```

### 維護者更新 Stable

```bash
cd /var/lib/jenkins/tomod/cosmetic-agent-stable

git status
git pull

bash /var/lib/jenkins/tomod/restart_api.sh
```

更新後檢查：

```bash
curl --fail http://localhost:8010/api/v1/health
```

查看 Stable Log：

```bash
cd /var/lib/jenkins/tomod
./team.sh logs stable
```

> 一般測試人員不可執行 Stable 的 `git pull` 或 `restart_api.sh`。

---

## TAIDE 模型操作

TAIDE 包含：

| Port | 模型 | Log |
|---:|---|---|
| `8000` | TAIDE 主模型 | `/tmp/taide_8000.log` |
| `8004` | TAIDE 膚況模型 | `/tmp/taide_8004.log` |

TAIDE GPU 模型平常保持長駐。

不要在一般測試結束後停止 TAIDE，因為：

- 多位成員共同使用。
- 模型重新載入約需兩分鐘。
- 停止 TAIDE 會影響其他正在測試的人。

只有容器重啟或 TAIDE 故障時才執行：

```bash
bash /var/lib/jenkins/tomod/restart_taide_servers.sh
```

查看 Log：

```bash
./team.sh logs taide-main
```

或：

```bash
./team.sh logs taide-skin
```

---

## 容器重啟後啟動全部服務

容器重新啟動後，執行：

```bash
cd /var/lib/jenkins/tomod
./start_all.sh
```

`start_all.sh` 會依序：

1. 啟動 TAIDE Main `8000`。
2. 啟動 TAIDE Skin `8004`。
3. 啟動 Stable API `8010`。
4. 啟動 Frontend `3000`。
5. 執行服務狀態檢查。
6. 執行健康檢查。

Development API `8011` 不會自動啟動。

需要 Development API 時另外執行：

```bash
./team.sh dev-start
```

> TAIDE 載入可能需要約兩分鐘。執行 `start_all.sh` 後請等待，不要連續重複執行。

---

## 連線方式

### 開發團隊使用 SSH Tunnel

在自己的電腦執行：

```bash
ssh -4 \
  -p 30892 \
  -L 3000:localhost:3000 \
  -L 8010:localhost:8010 \
  -L 8011:localhost:8011 \
  <SSH_USER>@140.119.24.206
```

請將：

```text
<SSH_USER>
```

替換成管理者提供的帳號。

連線完成後：

```text
Frontend：http://localhost:3000
Stable API：http://localhost:8010
Development API：http://localhost:8011
```

使用完畢後：

```text
按 Ctrl+D，或直接關閉 SSH Terminal
```

關閉 Tunnel 不會停止伺服器服務，因此不要因為離開而執行 `kill`。

> 不可將 root 密碼、SSH Key 或其他登入資訊寫入 README 或傳給未授權人員。

---

## 測試人員操作流程

測試前先確認服務：

```bash
cd /var/lib/jenkins/tomod
./team.sh status
./team.sh health
```

### 一般前端功能測試

如果服務都是 `RUNNING`／`OK`：

1. 開啟 SSH Tunnel。
2. 在瀏覽器開啟 `http://localhost:3000`。
3. 執行測試項目。
4. 記錄預期結果與實際結果。
5. 測試完畢後關閉 SSH Tunnel。
6. 不停止 Stable、Frontend 或 TAIDE。

### 後端開發測試

修改 `cosmetic-agent/` 後：

```bash
./team.sh dev-start
./team.sh health
```

查看錯誤：

```bash
./team.sh logs dev
```

測試完如果需要釋放 Development API：

```bash
./team.sh dev-stop
```

---

## 什麼時候做什麼

| 情境 | 操作 |
|---|---|
| 只要使用 Demo | 開 SSH Tunnel，瀏覽器開啟 `localhost:3000` |
| 查看服務是否正常 | `./team.sh status` |
| 執行健康檢查 | `./team.sh health` |
| 修改 Dev 後端後測試 | `./team.sh dev-start` |
| 查看 Dev 錯誤 | `./team.sh logs dev` |
| 停止 Dev 服務 | `./team.sh dev-stop` |
| 修改前端程式 | 儲存檔案，通常會自動熱重載 |
| 前端異常 | `./team.sh frontend-start` |
| Stable 發布 | Git Merge → Stable `git pull` → `restart_api.sh` |
| 容器重新啟動 | `./start_all.sh` |
| 測試或 Demo 結束 | 關閉 SSH Tunnel，不停止共用服務 |

---

## Port 與 PID

Port 是固定門牌，PID 是目前使用該 Port 的程序。

例如：

```text
Port 8011 = Development API 的固定位置
PID 3175193 = 目前執行 Development API 的程序
```

Development API 重新啟動後：

- Port 仍然是 `8011`
- PID 會改變

因此，Port 不會因為使用過就失效。

`address already in use` 表示舊程序仍占用該 Port，不代表 Port 壞掉。

請使用：

```bash
./team.sh dev-start
```

讓腳本自動停止舊程序並重新啟動，不要自行重複啟動多個 API。

---

## 查看 Log

```bash
# Frontend
./team.sh logs frontend

# Stable API
./team.sh logs stable

# Development API
./team.sh logs dev

# TAIDE 主模型
./team.sh logs taide-main

# TAIDE 膚況模型
./team.sh logs taide-skin
```

也可以直接查看：

```bash
tail -f /tmp/frontend_3000.log
tail -f /tmp/api_8010.log
tail -f /tmp/api_8011.log
tail -f /tmp/taide_8000.log
tail -f /tmp/taide_8004.log
```

按 `Ctrl+C` 只會停止查看 Log，不會停止服務。

---

## 手動健康檢查

```bash
echo "Stable:"
curl --fail --silent \
  http://localhost:8010/api/v1/health
echo

echo "Development:"
curl --fail --silent \
  http://localhost:8011/api/v1/health
echo

echo "Frontend:"
curl --silent \
  --output /dev/null \
  --write-out "HTTP %{http_code}\n" \
  http://localhost:3000
```

一般情況建議直接使用：

```bash
./team.sh health
```

---

## Git 開發流程

每位開發者應遵循以下流程：

```bash
cd /var/lib/jenkins/tomod/cosmetic-agent

git status
git switch -c feature/<功能名稱>
```

修改完成後：

```bash
cd /var/lib/jenkins/tomod
./team.sh dev-start
./team.sh health
```

確認功能正常後：

```bash
cd /var/lib/jenkins/tomod/cosmetic-agent

git status
git add <修改的檔案>
git commit -m "feat: describe the change"
git push -u origin feature/<功能名稱>
```

接著在 GitHub 建立 Pull Request。

禁止直接將未測試程式複製到 `cosmetic-agent-stable/`。

---

## 目前測試重點

詳細測試步驟請參考 [`TESTING.md`](./TESTING.md)。

目前包含：

1. 問卷完成後重新測驗，不應錯誤跳到商品推薦。
2. 問卷問題、預設回覆及 API 串接。
3. 今日小提醒使用 Zep Memory 保存。
4. 移除首頁 Chatbox 過度特定的快捷按鈕。
5. 膚況 Agent 左右臉及程度 Slider。
6. `team.sh`、`start_all.sh` 與服務健康檢查。

---

## 多人測試限制

每個瀏覽器會建立自己的 `blink_session_id`，因此不同使用者的：

- 對話
- Memory
- Session
- Agent 狀態

應互相隔離。

但 TAIDE GPU 模型的推論能力有限：

- 2–4 人同時使用：通常可以接受。
- 多人同時送出請求：可能排隊。
- 大規模測試：應分批進行。
- 100 人同時測試：目前可能造成模型服務阻塞。

Demo 階段建議一次安排少量測試人員。

---

## 安全注意事項

不可提交至 GitHub：

```text
API Key
密碼
Token
SSH Key
正式資料庫帳號
使用者隱私資料
模型檔案
大型 Log
環境變數檔案
```

建議 `.gitignore`：

```gitignore
# Environment
.env
.env.local
.env.test

# Python
__pycache__/
*.pyc
venv/

# Node
node_modules/
.next/

# Logs
*.log
logs/

# GraphRAG generated data
cache/
output/

# Backup
*.tar.gz
*.bak

# Models
taide-12b/
```

---

## 問題回報格式

```text
問題標題：

負責人：

測試日期：

Git Branch／Commit：

測試環境：
[ ] Frontend 3000
[ ] Stable API 8010
[ ] Development API 8011
[ ] TAIDE Main 8000
[ ] TAIDE Skin 8004

操作步驟：
1.
2.
3.

預期結果：

實際結果：

錯誤訊息：

Log：

截圖：
```

---

## 維護者

- 專案名稱：BLINK AI
- Stable 維護者：請填寫
- Frontend 維護者：請填寫
- Agent 維護者：請填寫
- 聯絡方式：請填寫

---

## 授權

```text
Copyright © 2026 BLINK AI Team
All rights reserved.
```
