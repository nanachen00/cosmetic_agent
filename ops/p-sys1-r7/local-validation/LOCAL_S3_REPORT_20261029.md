# S3 影子比對（synthetic）

> **限制**：來源是合成情境（scenarios.json）。合成句的分布、措辭與真實使用者不同；一致率與安全差異只說明接線與明顯錯誤，**不能**當成真實流量上的準確率或安全漏判率。
> 舊 router 的『安全』= production 規則（FIND_PRODUCT 且命中 SAFETY_KEYWORDS）；layer-1（含 119 緊急回覆）在 parse_intent 之前，不在本比對範圍。

- 列數 7（Sys1 成功 7，錯誤 0）· trace `runs\s3\trace.jsonl`
- 路由一致率（Sys1 成功列）：0.000 [95% CI 0.000–0.354]
- Sys1 錯誤率：0.000（0/7）
- **舊 router 判安全、P+Sys1 沒判：0 句**（逐句列在 `safety_llm_only.csv`，需要人工逐一看）
- P+Sys1 判安全、舊 router 沒判：4 句
- 延遲 p50/p95：Sys1 HTTP 12/75 ms · 含 Parser 14/86 ms · 舊 LLM parse_intent 1785/2941 ms
  （shadow 模式下 Sys1 在背景執行緒跑，與 LLM 呼叫同時；延遲可能被同機負載影響，canary 前請在 sys1 arm 再量一次）

## 混淆矩陣（列＝舊 LLM router，欄＝P+Sys1）

|llm＼sys1|safety|skin_tracking|product|knowledge|conversation|clarify|
|--|--|--|--|--|--|--|
|safety|0|0|0|0|0|0|
|skin_tracking|0|0|0|0|0|0|
|product|0|0|0|0|0|0|
|knowledge|4|1|0|0|0|1|
|conversation|0|0|0|0|0|0|
|clarify|0|0|0|0|1|0|

不一致清單：`disagreements.csv`（7 列；trace_text=false 時只有 q_sha）。
