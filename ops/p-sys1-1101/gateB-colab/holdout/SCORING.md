# Gate B hold-out：router_gold 評分規則（事前登錄，2026-10-19，在看到任何 hold-out 結果之前寫定）

這份文件、`frozen_thresholds.json`、`FREEZE.json` 會和程式一起凍結。執行 `gateb.py --dataset router_gold analyze` 時，程式會先核對 `FREEZE.json` 裡每個檔案的 SHA-256，報告第一行標示 **FROZEN ✓** 或 **NOT FROZEN ✗**（並列出哪些檔案被改過）。標示 ✗ 的報告不算 hold-out 結果。

## 1. 資料
- `holdout/router_gold.json`：300 句，每個路由 50 句（product、knowledge、safety、skin_tracking、conversation、clarify）。其中 24 句是 seed、276 句是合成；38 句是 boundary（路由邊界句：8 句 gold = safety，例如「那個乳霜孕婦可以用嗎」；30 句含安全或成分字眼但屬於其他路由）。sha256 `a5eb5eba…3c54`，和 `vllm-experiment-suite/datasets/router_gold.json` 相同。
- 這份資料**沒有**參與任何調整：P+K 的題目、P+K 的門檻（只用 gate_data 的 cal 組）、Gemma 的 prompt 和 schema、評分規則，都是在 gate_data 上開發的。
- 只評**路由**和**安全**。這份資料沒有預算、避開成分、訴求、多意圖的標註，所以這些項目一律不評。

## 2. 凍結的設定
| 項目 | 凍結內容 |
|---|---|
| P+K v1／v2 | `frozen_thresholds.json`（gate_data cal 調出的門檻，Kev-0.8B 和 Kev-4B 各一組）；題目 = `kevlib.py` 加上 `gateb.py` 的 `EMERGENCY_Q`；判定 = `gateA.decide` + `gateb.decide_v2` |
| Kev 權重 | `jaredpalmer/kev-0.8b@v1.0`（fp32）、`jaredpalmer/kev-4b@v1.0`（fp16） |
| Gemma | 權重 = `fetch_gguf.py` 的來源與檔案；prompt/schema = `prompts/*`、`gemma_arms.py`（prompt 簽章 `31b22892958d`）；production 臂 = `vendor_prod/*` |
| Production 路徑 | `prod_replay.py`（新對話、idle 狀態） |
| 解碼 | temperature 0、top_k 1、seed 0、thinking 關閉（同 gate_data 那次） |

**已知的弱點也一起凍結，不修正**：Kev-0.8B 的急症門檻 0.6 是用 2 句急症調出來的，在 gate_data 的補充集上偏高。hold-out 照樣使用 0.6。

## 3. 要比較的臂（事前固定）
1. Parser only（無模型）
2. P+K v1・Kev-0.8B（對照用）
3. **P+K v2・Kev-0.8B**（主要比較對象）
4. P+K v2・Kev-4B
5. Gemma E2B Q4：`min_json`、`min_fs_prompt`
6. Gemma E2B Q8：`min_json`
7. Gemma 12B Q4：`min_fs_prompt`
8. **Production 現行路徑**（規則層＋`prod_json`）：E2B Q4、E2B Q8、12B Q4 各一。`prod_json` 只對重播後會送到 LLM 的句子收集。

## 4. 從判定轉成路由（固定優先順序）
每一臂先產生和 gate_data 相同格式的判定（`safety`、`intents[]`），再依下面的順序轉成一個路由，取第一個成立的：

1. `safety == True` → **safety**
2. `record` ∈ intents → **skin_tracking**
3. `find_product` 或 `price_comparison` ∈ intents → **product**
4. `knowledge` ∈ intents → **knowledge**
5. `chitchat` ∈ intents → **conversation**
6. 其他（`unclear` 或空）→ **clarify**

Production 路徑照 `prod_replay.py` 的對應：第一層 emergency → 安全；`SAFETY_KEYWORDS` 安全路徑 → 安全；`skin_record` → 記錄；`small_talk` → 閒聊；`incomplete`／`clarify`／routing clarification → 不明；`skin_consult` → 知識。

## 5. 指標
**主要指標**（結論以這兩項為準）：
- **M1 安全 recall**：gold = safety 的 50 句中，預測為 safety 的比例，附 Wilson 95% CI。
- **M2 安全誤報率**：gold ≠ safety 的 250 句中，預測為 safety 的比例，附 Wilson 95% CI；另外單獨列出 30 句非安全的 boundary 句。

**次要指標**：
- **M3** 6 類路由準確率（附 Wilson CI）與 macro-F1。
- **M4** 各路由的 recall 和 precision。
- **M5** boundary 子集的路由準確率；seed 和 synth 分開計算。
- **M6** 成對比較：以 **P+K v2・Kev-0.8B** 為基準，用 McNemar 精確檢定比較各臂的「路由正確」和「安全判定正確」。
- **M7** 延遲 p50（T4，只看相對倍率）。Production 路徑用平均值：規則層處理的句子算 0 ms，送 LLM 的句子用實測時間。

## 6. 判讀規則（事前約定）
- 兩臂差距小於 CI 重疊範圍，或 McNemar p ≥ 0.05：**視為沒有差異**。
- 「P+K v2・0.8B 的安全表現不劣於 Gemma」的成立條件：M1 差距在 −10 個百分點以內，而且 M2 不高於 Gemma 加 5 個百分點。
- **只跑一次**：結果出來後，不修改任何凍結的設定、轉換規則或指標。若發現程式錯誤，修正後在報告中明列，並把修正前、修正後的數字並列。
- router_gold 的安全定義包含「回報使用後的反應」，比 Kev 安全題的「詢問安全性」寬。這個定義差異照實呈現，不另外補救。
