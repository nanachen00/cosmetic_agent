# BLINK AI 平台操作入口

目前正式架構是：Mac 執行 Next.js 前端與 FastAPI 後端，學校 MI300X
執行 embedding 與照片模型，模型 API 只透過 SSH loopback forwarding 存取。

## 日常操作

從專案根目錄執行：

```bash
BLINK_RUNTIME_ROLE=mac ./ops/runtime/start-production.sh
BLINK_RUNTIME_ROLE=mac ./ops/runtime/status-production.sh
BLINK_RUNTIME_ROLE=mac ./ops/runtime/stop-production.sh
```

目前服務：

| Port | 執行位置 | 用途 |
|---:|---|---|
| `3000` | Mac | Next.js 網站 |
| `8010` | Mac | FastAPI 後端 |
| `6389` | Mac | Redis admission、狀態與限流 |
| `8002` | MI300X，經 SSH 轉發到 Mac loopback | BGE-M3 embedding |
| `8025` | MI300X，經 SSH 轉發到 Mac loopback | Qwen 照片分析 |

TAIDE 與舊 router 預設停用。模型埠不可直接公開，也不要用臨時 uvicorn、vLLM
或背景指令繞過 `ops/runtime` 控制腳本。

## 文件來源

- 部署與 SSH 通道：[ops/runtime/SPLIT_DEPLOYMENT.md](ops/runtime/SPLIT_DEPLOYMENT.md)
- 架構入口：[cosmetic-agent/docs/architecture/README.md](cosmetic-agent/docs/architecture/README.md)
- 最新交付與驗收：[cosmetic-agent/docs/architecture/PLATFORM_DELIVERY_PLAN.md](cosmetic-agent/docs/architecture/PLATFORM_DELIVERY_PLAN.md)
- 容器重置復原：[cosmetic-agent/docs/architecture/CONTAINER_RESET_RECOVERY.md](cosmetic-agent/docs/architecture/CONTAINER_RESET_RECOVERY.md)
- 模型版本與校驗：[ops/runtime/models.manifest.json](ops/runtime/models.manifest.json)

`cosmetic-agent/.env` 是 Mac 後端與認證唯一設定檔。不要將密鑰、資料庫、會員資料、
模型權重、checkpoint、log 或 runtime 產物加入 Git。

舊的一體式 GPU 前後端、`8011` 開發服務、`8003/8004` 與
`/var/lib/jenkins/tomod` 操作說明已移除，避免誤啟動歷史拓撲。
