# Laya 本機契約檢查（2026-11-03，本機模擬：Windows CPU）

- 環境：`D:\devtools\venvs\laya-local`（Python 3.12.14、torch 2.6.0+cpu、`laya[serve]==0.4.21`、transformers 5.22.1）。VM 上的 `/opt/venv-laya` 會把 transformers 鎖在基底的 4.57.6，版本不同。
- 入口：`sys1/laya_entry.py --port 8231`，`LAYA_MODELS=multilingual,typed-decisions`、`LAYA_DEVICE=cpu`、`LAYA_PRELOAD=1`。檢查點從 Hugging Face 下載。
- 結果（已驗證，本機模擬）：

| 檢查 | multilingual（laya-mmbert） | typed-decisions（laya-typed） |
|--|--|--|
| `GET /v1/models`（轉接層補上） | 200 | 200 |
| `run_cand.py probe`：11 題都有回答、機率範圍、choice 有 probabilities | 通過 | 通過 |
| `contract_check.py`：noul 方向（安全句平均 vs 非安全句） | 0.887 vs 0.368，是＝高 ✓ | **不對**（英文檢查點，中文預期很差） |
| 伺服器自報延遲寫進 `latency_ms` | 有（約 1.9 秒） | 有（約 6.8–8.1 秒） |

- 延遲是 CPU 的數字，沒有參考價值；MI300X 的數字以 VM 上 `DEVICE=cuda ./run_vm.sh laya-contract` 為準。
- 沒有收集 gate_data 或 router_gold（hold-out 只在 VM 上看一次）。
- 啟動時 Laya 警告 multilingual 檢查點有一個超出範圍的溫度值（`choice:11+`），被夾到 0.5；Laya 說受影響的 confidence 視為未校準。
