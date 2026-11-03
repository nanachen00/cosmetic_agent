# 交付驗證：並行部署容忍的 Shadow 測試工具

**本報告為本機 unit／loopback／受控子程序 fixture，不是使用者 server Smoke 或 MI300X 效能結果。** 未連線真實會員、DB、Zep、TAIDE或vLLM，不使用真實模型權重。

| Suite | 實際結果 |
|---|---:|
| `tests/test_bundle.py`：既有pidfd、gateway、schema、隔離、Smoke/path回歸 | 113／113 PASS |
| `tests/test_auth_workflow.py`：固定會員、guest、匿名、CookieJar、session、logout | 35／35 PASS |
| `tests/test_concurrent_deploy.py`：固定副本、擷取競態、部署epoch、單workflow、owned安全 | 63／63 PASS |
| `tests/test_concurrent_requests.py`：實際loopback會員HTTP＋批次干擾／未送出批次、模型metadata與pack | 8／8 PASS |
| 外附安裝器：新裝、會員版更新、來源metadata更新版、鎖、rollback、私密保留 | 14／14 PASS |
| **合計** | **233／233 PASS，沒有以 skip 算 PASS** |

內附 `selftest.sh` 執行前四個 suite，共219項。安裝器測試另外針對交付archive執行，不會在server selftest再次安裝工具。

## 真正有測到的路徑

- 對獨立bytes擷取、來源與config擷取中變動、有界重試、擷取後正式source被刪除或修改，已用filesystem fixture重現。
- 實際執行prepare/config helper建立capsule；服務capture用fixture資料，不 import真實backend。擷取後兩個cell仍得到同一份來源。
- auth/smoke/formal 用同一個 Experiment 與 snapshot ID；auth或Smoke不通過不進Formal。沒有偷偷再capture正式source。
- 用fake auth HTTP server真實測cookie／session／revoked-cookie行為，member與guest分開；登入錯誤不重試、不退回guest。
- 不可用的某批沒有送出聊天；下一批用新conversation，確認訊息計數沒有被重試加倍。epoch變動即使HTTP成功仍標干擾。
- 正式PID更換不呼叫shared stop/restore；:8200受保護。外部程序接手owned port或數字PID重用，不向它發signal。
- pidfd：保留原Python／libc／經核對ABI syscall介面、權限錯誤不降級、真正的測試專用child優雅停止。
- 局部console CLI credential遮蔽、private capsule不可打包、snapshot config symlink拒絕、多個production root的source寫入拒絕。
- owned vLLM只測argv建構／顯示卡記憶體計算／model file metadata fixture／policy；**沒有啟動真實vLLM，也沒有驗證實機32／0.5一定可用**。
- 安裝器真實解開交付archive到temporary ops目錄，驗證原會員bundle及新增source-contract metadata的兩種既有狀態；私密帳號與結果原樣保留。注入寫檔例外驗證rollback。

## 既有測試的替換說明

原會員版的116個基礎測試中，有3個只測舊shell「分開四次runner／重新擷取／舊cleanup gate」；架構改成單runner後不保留錯誤假設，由新的single-capsule、auth／Smoke gates、部署干擾與owned-cleanup測試取代。另將候選stop／PIDfile更新測試改成**必須拒絕shared操作**。不是為了通過而關閉安全斷言。

## 不宣稱的事情

- 不能證明真實會員／模型／資料庫的Smoke已通過，或所有未來source都相容。
- 兩次穩定scan不是原子release snapshot，不證明多檔部署語意完整。
- runtime METADATA／權重metadata不等於所有package與weight bytes不可變。
- process epoch取樣不證明沒有短暫中斷、DB schema/data改動或GPU競爭。
- 同一snapshot不等於會員長期記憶或DB狀態相同。
- 保守GPU admission不保證不發生OOM或外部程序不再配置VRAM。
- SIGKILL／斷電／container消失不保證finally或帳號session清理。

所有Python語法、Shell bash -n、Manifest、重新解壓後內容與CLI help另外檢查。最後的隔離模式CLI檢查發現新模組的import順序問題，已修正並加入從無關工作目錄以 `python -I -B bench.py --help` 執行的回歸測試；不能只以import過的unit tests取代入口測試。`VALIDATION.json`保留結構化範圍；外附logs是合成本機證據，不進server量測。
