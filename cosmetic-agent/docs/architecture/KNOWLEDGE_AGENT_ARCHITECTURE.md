# Knowledge 回答流程架構

本文件記錄目前正式 Knowledge 回答路徑的責任邊界，避免後續維護時重新接回舊模型路徑，或把資料庫、模型 SDK 與業務流程再次混在同一個檔案。

## 1. 現行流程

```text
使用者請求 / Root Agent
        │
        ▼
KnowledgeSubAgent
（歷史、意圖、檢索編排、證據整理、提示詞、觀測、答案保存）
        │
        ├─ 成分抽取
        │    └─ ingredient_extractor
        │         └─ ChatCompletionClient
        │              └─ LiteLLM :8003 / TAIDE 模型
        │
        ├─ 知識檢索
        │    └─ KnowledgeRetrievalService
        │         ├─ KnowledgeGraphRepository ── Neo4j
        │         └─ ingredient / skin concern / risk 工具
        │
        └─ 最終回答
             └─ KnowledgeResponseClient
                  ├─ Terra Responses API（正式環境預設）
                  └─ TAIDE Chat Completions（可切換備援路徑）
```

`:8200` 的 vLLM 目前仍是 `EXPERIMENTAL / UNMANAGED`，不屬於上述正式 Knowledge 回答路徑；只有日後明確修改環境設定與部署管理範圍後才可接入。

## 2. 各層責任

### KnowledgeSubAgent

只負責回答流程的編排：

- 取得與整理對話歷史。
- 判斷問題意圖。
- 呼叫既有知識檢索工具。
- 將 Neo4j 與規則結果整理成模型證據。
- 維持現有 system prompt、訊息格式與回答保存流程。
- 記錄各階段時間、錯誤與 request context。

它不再直接管理 OpenAI SDK，也不再直接建立 Neo4j driver。

### KnowledgeRetrievalService

專門編排 Graph RAG 檢索：

- 建立每次請求的乾淨 context。
- 固定將 `risk_rules` 排在成分檢索之後。
- 集中處理成分抽取、query expansion、膚況與風險規則。
- 保持既有 stage timing 名稱、session 關閉與例外傳遞行為。

### KnowledgePromptBuilder

純粹負責模型輸入內容：

- 將 ingredient、skin concern 與 warnings 整理成 evidence。
- 建立 system prompt 與記憶內容。
- 不依賴資料庫、網路或模型 SDK。
- 以搬移前輸出的 SHA256 與長度鎖定，確保 Prompt 逐字一致。

### KnowledgeResponseClient

專門處理模型供應商通訊：

- 根據 provider 建立正確的 SDK client。
- 組裝 Terra Responses API 或 TAIDE 相容 Chat Completions payload。
- 統一同步與串流回傳。
- 過濾空 delta，並保持既有 callback 順序。
- 管理 timeout、retry、model 與 token 上限。

### KnowledgeGraphRepository

專門處理 Neo4j 存取邊界：

- 從環境變數讀取並驗證連線設定。
- 建立與關閉 driver。
- 提供 session lifecycle。
- 保持既有 URI 預設值、驗證錯誤與 driver 參數。

Cypher 查詢與知識規則目前仍由既有工具負責；本階段沒有改寫查詢結果或排序。

### KnowledgeConversationHistory

專門管理 Knowledge 對話的暫存歷史：

- 依 session 隔離訊息。
- 保留原本最多 20 輪的裁切規則。
- 提供加入、清除與取得上一則使用者問題。
- Agent 的 `conversation_history` 與 `max_history_turns` 暫時保留為相容欄位。

### KnowledgeResponseConfig

統一把環境變數轉成明確設定物件：

- provider 與 model。
- base URL 與 API key。
- timeout 與 retry。
- max output tokens。
- Terra reasoning effort。

## 3. 正式預設與可切換設定

| 項目 | Terra 正式預設 | TAIDE 相容路徑 |
|---|---|---|
| Provider | `KNOWLEDGE_RESPONSE_PROVIDER=terra` | 設為非 `terra` 值時使用相容路徑 |
| Model | `gpt-5.6-terra` | `taide-12b` |
| API | Responses API | Chat Completions API |
| Base URL | OpenAI SDK 正式端點 | `http://localhost:8000/v1` |
| Timeout | 45 秒 | 60 秒 |
| Retry | 1 次 | 0 次 |
| Max output tokens | 384 | 384 |
| Reasoning effort | `none` | 不使用 |

實際值可由既有環境變數覆寫；預設值刻意維持重構前的執行行為。

## 4. 相容性保證

這一輪只建立邊界，沒有改變回答結果：

- system prompt 與使用者訊息內容未更動。
- 回答文字、串流 callback 與歷史資料形狀未更動。
- 知識檢索、Cypher、排序與風險規則未更動。
- 暫時保留 `self.client`、`self.neo4j_driver` 與 `neo4j_*` 欄位，避免既有呼叫端突然失效。
- 未知 provider 仍沿用原本 TAIDE／OpenAI-compatible 分支。
- 本次重構沒有重啟正式服務；線上程序仍使用重啟前已載入的版本。

## 5. 驗證

已覆蓋：

- Terra 與 TAIDE 設定預設值、環境變數覆寫。
- 兩種 provider 的 SDK 初始化與 payload。
- 串流與非串流解析。
- delta 過濾與 callback 次序。
- Neo4j 設定驗證、driver、session 與 close delegation。
- 成分抽取共用 client。

最新全專案回歸結果：`293 passed, 1 skipped, 10 subtests passed`。`pytest.ini` 已固定專案根路徑，Terra 測試不再注入 Zep／psycopg 假模組；Python `compileall` 與 `git diff --check` 均通過。Zep 與 PostgreSQL 驅動改為實際使用時才載入，未啟用可選服務時不再阻斷主程式或測試匯入。Knowledge Agent 仍維持 Terra 回答、TAIDE 可選路徑及既有輸出行為。

## 6. 部署與維運注意事項

- 程式碼變更只會在下一次受管控的 production restart 後載入。
- 重啟應只使用 `/var/lib/jenkins/tomod/ops/runtime/restart-production.sh`。
- 重啟後必須執行 `status-production.sh`，確認 `:8010/readyz`、PID、命令與工作目錄。
- 不應直接以臨時命令另啟一份 Knowledge、TAIDE 或 Embedding 程序。
- `:8200` 在納入三支 production 腳本前，不可視為正式依賴。

## 7. 下一個安全重構階段

1. 已用 fake repository／tool 補齊 `KnowledgeSubAgent` 編排測試，並鎖定歷史裁切、檢索順序、空結果、工具失敗、證據傳遞與答案保存。
2. 已將 in-memory history 規則移入 `KnowledgeConversationHistory`，相容欄位仍保留。
3. 已將 Graph RAG 移入 `KnowledgeRetrievalService`，提示詞移入 `KnowledgePromptBuilder`；Prompt 內容逐字不變。
4. 下一步分別診斷 5 個 skin tracking 舊測試與 2 個 TAIDE／prompt contract 舊測試，不以回退正式架構換取測試通過。
5. 全專案搜尋確認沒有外部呼叫後，才移除暫留的相容欄位。
6. 在使用者核准的部署時段做受管控重啟與實際聊天 smoke test。

在上述測試完成前，不繼續把 Agent 拆成更多小類別，避免為了目錄整齊而增加不必要的跳轉與維護成本。
