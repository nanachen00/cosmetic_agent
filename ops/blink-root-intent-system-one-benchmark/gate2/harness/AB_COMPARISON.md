# :8000 / :8200 A/B outcome-completion correction

本版恢復 handoff 原始規則：provider-local 的 schema/contract/transport gate failure 是該 treatment 的正式結果，停止該 provider 後續 Formal/升載，但不取消另一個 provider。只有 shared auth、identity/policy、owned cleanup、runtime drift 或未分類錯誤才停止整個實驗。

`:8000` 若在三案例 Smoke 留下完整 endpoint、identity、contract、stage 與 cleanup 證據但 contract FAIL，會保留 `SMOKE_GATE_FAILED`，接著 cooldown 後使用同一 backend snapshot 執行 `:8200` Smoke + Formal VU1/4/8。不得把 `:8000` FAIL 改寫成 PASS，也不得用歷史 `:8200` 拼接。

`full_comparison_ready` 現在表示兩個 treatment 都有乾淨、完整的終局證據；`functional_all_planned_pass` 仍只在全部 Formal 都通過時為 true；新增 `full_performance_comparison_ready`，只有兩組 Formal 全部完成且 performance eligible 時才為 true。資料不完整前仍不產生互動式 HTML。

---

# BLINK Shadow :8000 / :8200 — 明示的 intent 輸出額度實驗

## 本輪依據與修正範圍

20260926-130043-646863 的 :8000 Smoke 為 1/3 通過；兩筆失敗 intent 呼叫皆回報 completion_tokens=400、requested_max_tokens=400、finish_reason=stop，且原始 JSON 解析失敗。Product 另未到預期 terminal stage。兩組 Formal 均未執行；:8200 未執行。這是輸出額度可能不足的線索，不是已證明截斷，亦不能證明放大額度就會修好語法。

本版不再更動 prompt。保留 legacy_schema_fields_v1 與 vllm_native_raw_query、完整原始 schema、raw_query 綁定、sampling、既有 timeout 與重試設定。新增明確啟用的 `--intent-output-token-floor 1024`：兩組均只對已識別且綁定原文的 `intent_parse_result` 使用 `max(原始輸出額度, 1024)`。其他 schema、一般文字與 witness 額度不變。不指定分類答案、不修輸出、不增加模型呼叫、不自動切換 profile。

1024 是本輪預先指定的受控實驗值，不是量得的最低需求，也不是共用模型服務參數修改。原始 caller=400、gateway effective=1024 與 proxy 真正送出的值分開記錄。每個相關 outgoing attempt 都須符合設定；掉參數或被 proxy 改回 400，仍 FAIL。此證據不能證明模型服務內部沒有再次限縮；若實測仍固定輸出 400，需再查內部限制，不能假定已生效。

Python JSON 診斷補上 invalid_unicode_escape 與不含原文字串的括號／字串／跳脫完整性計數。它們不作為替代 parser，不更改原始回覆或 schema verdict。既有 no_truncated_generation 只代表未回報 length/content_filter，不等於已排除所有截斷。

## 安裝與執行

所有預設作業、安裝暫存及回歸測試暫存維持原目錄；不用 /tmp 或 /temp：

```bash
cd /var/lib/jenkins/tomod/ops/local-llm-path-benchmark
python3 ./apply_shadow_ab_update.py --check &&
python3 ./apply_shadow_ab_update.py --apply
```

更新器使用 `.maintenance/` 和 `backups/`；安裝不啟動 server 實測。不覆蓋 config.json、帳號、歷史結果或模型，不修改正式 :8010，不啟停 shared :8000/:8200。遇到鎖或版本衝突，停止而非強制覆蓋。

```bash
cd /var/lib/jenkins/tomod/ops/local-llm-path-benchmark
bash ./run_verified_shadow_tests.sh \
  --ack-shared-data-services \
  --auth-mode member \
  --accounts-file .private/accounts.json \
  --vllm-mode shared \
  --providers 8000,8200 \
  --schema-mode provider_specific \
  --legacy-contract-mode legacy_schema_fields \
  --intent-output-token-floor 1024 \
  --vus 1,4,8 \
  --repetitions 1
```

新增參數必須明確提供；未提供即保留原額度，不會默默開啟。請勿混用舊 run 的延遲，這是新一輪生成預算條件。

固定一次 snapshot → 共用會員 Auth → :8000 三個 Smoke → 通過後 :8000 Formal VU1/4/8 → cooldown → :8200 Smoke → 通過後 :8200 Formal VU1/4/8 → owned cleanup → JSON/CSV 證據包。

:8000 未通過就停止、不跑 :8200、不自動重試至成功。兩組各 39 筆 Formal，共 78 筆；Smoke/witness/control 不混入。既有 timeout 不延長：若較長生成觸發原有 timeout，保留 timeout failure，不用放寬 timeout 取得 PASS。

## 回傳

不產生互動式 HTML。只上傳 console 列出的 `[A/B SHARE ARCHIVE] .../<timestamp>-ab-results.tar.gz`；內容仍僅 report-data.json、batch-comparison.csv、request-comparison.csv、EVIDENCE.md、validation.json。不要重新封存 .private、.state、session、環境、raw log 或模型。

只有本輪兩組完整 Formal 與 path/identity/endpoint/snapshot/cleanup 等證據成立，才標示 full_comparison_ready=true。退出碼 2 依診斷判讀；90 先處理 owned cleanup，不刪鎖繞過或停止 shared service。

## 驗證界線

本機只驗證程式、合成回覆、loopback HTTP、guarded ASGI proxy、併發與安裝回復。沒有真實 TAIDE/GPU、server 會員或外部依賴存取；測試通過不代表模型已能正確生成 JSON。詳細數量與 SHA-256 以同批本機驗證 JSON 為準。
