# 來源與設計差異

基底：上一輪固定會員版harness。`source_contract.json`保留`source-delta-20260925-095202-560571-3bd879`已審查29項雜湊，**只作歷史差異，不是新正式版本的阻擋白名單**。歷史資料不代表server即時狀態。

保留會員session/撤銷、匿名guest對照、原身分回傳觀測、system合併、prompt_only/schema／截斷／fallback／terminal stage驗證、pidfd syscall相容、精確Neo4j目的地、platform查詢規則與獨立Redis。

新增：frozen_snapshot（一次byte副本、bounded retry、內容ID），runtime_capture（實際launch及METADATA），deployment_observer（shared epoch和unknown），owned_vllm（可選新模型與保守記憶體gate）。

變更：bench與workflow只捕捉一次；profile每批記snapshot／interference；process_control把8200列protected、舊candidate動作永遠拒絕。cleanup不以正式source/PID漂移當失敗，不嘗試把團隊deployment還原成舊版。

正式應用程式沒有為了測試而被修正。缺API/import、無法驗證會員、schema失敗仍可停止，不宣稱所有未來版本都保證相容。

官方參考（版本敏感項目固定v0.9.2）：

- vLLM記憶體參數：`https://docs.vllm.ai/en/v0.9.2/api/vllm/entrypoints/llm.html`。
- Linux pidfd：`https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html`。

先前要求所有正式PID/hash不變的舊測試替換為shared永不控制、owned僅清自己、外部接管不誤停、相同snapshot貫穿所有階段。測試工具來源基準安裝檢查仍在，與正式source歷史SHA gate是不同事情。
