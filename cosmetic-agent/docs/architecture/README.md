# 架構文件入口

目前部署方向：Mac 前後端、學校 MI300X 訓練與推論、模型 API 經 SSH loopback forwarding。

- [交付順序、驗收條件與最新狀態](PLATFORM_DELIVERY_PLAN.md)
- [學校 GPU 容器重置後的復原手冊](CONTAINER_RESET_RECOVERY.md)
- [部署操作](../../../ops/runtime/SPLIT_DEPLOYMENT.md)
- [ASGI 與模組所有權](../../ARCHITECTURE.md)
- [初次稽核基線](DEPLOYMENT_AUDIT_2026-11-15.md)

早期文件的 `/var/lib/jenkins/tomod`、遠端前後端一體式部署與 8003／8011 說明屬歷史記錄。
模型檔完整性仍以 `ops/runtime/models.manifest.json` 與實際檔案檢查為準。
