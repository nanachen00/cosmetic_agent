# AI Stack 整理規則

目前不直接搬動或刪除 `/var/lib/jenkins/tomod` 的任何檔案，先用唯讀盤點產生 `/tmp/blinkai_ai_stack_inventory.json`。

建議最終結構：

```text
/var/lib/jenkins/tomod/
├── apps/
│   ├── backend-dev/
│   ├── backend-prod/
│   └── frontend/
├── ops/
│   ├── deploy/
│   ├── restart/
│   └── healthcheck/
├── models/
├── tests/
├── logs/
├── backups/
└── archive/
```

## 執行順序

1. 保護目前執行中的 `cosmetic-agent`、`cosmetic-agent-stable`、`frontend`，不更名、不移動。
2. 檢查所有 restart/deploy 腳本引用的絕對路徑。
3. `.bak`、`.old`、壓縮檔只列為 archive candidate；先比對 Git 與備份內容。
4. log 先確認沒有被監控或啟動腳本引用，再移到 `logs/` 或設定 log rotation。
5. 先搬到有日期的 `archive/`，觀察一個發布週期後才考慮刪除。
6. runtime 目錄更名必須另開維護時段，同步更新 systemd、Nginx、Jenkins 與部署腳本。

任何刪除都要另行取得確認；本輪只分類與產生報告。
