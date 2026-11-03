# AI Stack 根目錄盤點（2026-09-09）

此報告由 Jupyter Contents API 唯讀取得；本輪沒有刪除、搬動或更名任何伺服器檔案。

## 執行中／必須保護

- `frontend/`：Next.js 開發前端。
- `cosmetic-agent/`：8011 development backend。
- `cosmetic-agent-stable/`：8010 production backend。
- `taide-12b/`、`pytorch/`、`venv/`：模型與共用執行環境，未確認引用前不可移動。
- `logs/`、`cache/`、`prompts/`、`supervisor/`：執行支援目錄。

## Ops 腳本（應集中到 `ops/`，目前先不搬）

- Production：`deploy-backend-prod.sh`、`restart-backend-prod.sh`、`restart_api_prod.sh`。
- Development：`restart_api_dev.sh`、`stop_api_dev.sh`、`restart_frontend.sh`。
- 共用／待確認：`start_all.sh`、`restart_api.sh`、`team.sh`、`restart_taide_servers.sh`。

搬動前必須先搜尋 Jenkins、Nginx、Supervisor、README 與其他 shell 腳本中的絕對路徑引用。

## 可封存候選（先比對，不直接刪除）

- `blink-regression-tests.tar.gz`
- `blink-monitoring-bundle.tar.gz`
- `blink-member-auth-route-fix-20260907.tgz`
- `blink-account-api-path-fix-20260907.tgz`
- `frontend-before-feature8.tar.gz`
- `cosmetic-agent_export.tar.gz`
- `skin-tracking-before-b21a2dd.tar.gz`
- `blink-auth-stage-20260906.tgz`
- `blink-auth-stage-20260906-v2.tgz`
- `restart_api.sh.bak-before-unify-20260906`
- `taide_server_graphrag.py.bak`
- `taide_server_skin_tracking.py.bak`
- `api_server.py.before-feature8`

建議先移至 `archive/2026-09-09/`，保留一個發布週期並確認可回復後，才另行核准刪除。

## Log 候選

- `embed.log`
- `chat.log`

確認沒有程式直接使用這兩個路徑後，移到 `logs/` 並設定 rotation；不可只刪檔而讓執行程式繼續無限新增。

## 來源待確認

- `src/`、`core/`、`input/`、`output/`
- `api_server_embedding.py`
- `taide_server_skin_tracking.py`、`taide_server_skin.py`、`taide_server_graphrag.py`
- `taide_fix_server.py`、`taide_router_8001.py`、`taide_server.py`
- `install_monitoring.py`、`monitoring_requirements.txt`
- `install_regression_tests.py`、`regression_tests/`、`run_regression_tests.sh`

這些可能仍被 TAIDE 或監控流程引用，分類為「待引用掃描」，不能直接判斷多餘。

## 下一個安全動作

1. 讀取所有啟動／部署設定引用的路徑。
2. 為候選檔計算 SHA-256，確認壓縮包內容已有 Git 或 backups 副本。
3. 只做可回復的 archive 移動。
4. 重啟 development 8011 與 frontend，確認功能後觀察一個週期。
5. 取得團隊確認後才刪除 archive。

