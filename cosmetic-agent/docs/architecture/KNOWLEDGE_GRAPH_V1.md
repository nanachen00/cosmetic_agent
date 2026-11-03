# BLINK AI 美妝保養知識圖譜 v1

狀態：draft / feature flag off / 尚未寫入 production Neo4j

## 目標

這不是固定畫面資料，而是可查詢、可擴充、可追溯的屬性圖。使用者輸入口語詞時，系統先做實體解析，再從標準節點展開一到兩跳關係。視覺化只是查詢結果的呈現方式，不是知識圖譜本身。

參考的知識圖譜概念與建構流程包含「實體－關係－實體」三元組、本體層、實體對齊、關係抽取、資料融合、推理與品質管控：<https://www.ernestchiang.com/zh/notes/general/knowledge-graph/>。

## v1 資料範圍

- 100 個 Ingredient：以 BLINK 8,939 筆產品的成分出現頻率為主，再補入壬二酸、傳明酸、氧化鋅、二氧化鈦、尿素、杏仁酸等常用核心成分。catalog_product_count 與 catalog_rank 保留選入依據；零商品連結不代表成分不常見，而是現有商品關聯尚未補齊。
- 47 個去重後的 SkinConcern：保留繁中、英文、標準鍵與口語別名。
- 14 個 Function 與 9 個 ConcernCategory：構成本體層，讓新成分可以先接到功能，再透過照護路徑連到問題。
- 55 個既有 RiskRule：只保留與本輪 100 成分相連的規則，未具 edge-level 引用者標記為 needs-review。
- TFDA 外用藥子圖：8 個獨立 DrugActiveIngredient、17 個 AliasTerm、53 個 DrugProduct、8 個台灣 Manufacturer、46 個 ApprovedIndication 與 4 個 Symptom。藥證資料只選非註銷、台灣製造且屬乳膏／軟膏／外用液／外用凝膠／洗劑的代表記錄；不是市占或推薦排行。
- 合計 361 節點、778 關係，沒有孤立節點或缺少端點的邊。

資料檔：data/knowledge_graph/v1/knowledge_seed.json

## 運作原理

### 1. 實體解析（Entity Resolution）

所有輸入先做 Unicode NFKC、大小寫及標點正規化，再依別名索引尋找標準節點。解析不是在前端寫死按鈕；任何呼叫 /api/knowledge/explore 的客戶端都共用同一套邏輯。

範例：

- 酒糟肌、酒糟、酒糟性皮膚炎、玫瑰痤瘡、酒糟鼻、rosacea
- 全部解析成 concern_rosacea
- 我有酒糟肌 也能以「查詢包含已知別名」解析

解析階段：

1. 種子圖譜精確別名。
2. 種子圖譜包含比對與英文 token 比對。
3. 既有 Neo4j 文字與 aliases 查詢。
4. 後續可加入 embedding 或小型分類器產生候選節點與信心分數；TAIDE 只保留為這一層的實驗候選，低信心不得直接建立事實。

### 2. 圖譜展開

GET /api/knowledge/explore?q=酒糟肌&depth=2&limit=60

解析後從標準節點做有界 BFS，預設展開兩跳：

    酒糟肌
    ├─ RELATED_CONCERN → 敏感肌、臉部泛紅、屏障受損、光敏感…
    ├─ SUITABLE_FOR_CONCERN ← 壬二酸（既有關係，needs-review）
    ├─ RELEVANT_TO_CARE ← 舒緩照護、屏障支持（本體導覽，ontology-review）
    └─ USE_WITH_CAUTION_FOR ← 角質調理、A 類照護（本體導覽，ontology-review）
       └─ HAS_FUNCTION ← 對應的一百個成分節點

因此畫面可以真的顯示「問題 → 相關問題／成分／功能／風險規則」，不是預先畫一張酒糟肌圖片。

### 3. 視覺分組（Progressive Disclosure）

底層保留完整關係，畫面預設只顯示中心節點與有資料的分類群組：常見叫法、含此有效成分的藥品、台灣製造廠、TFDA 核准適應症、對應問題／症狀、成分類型／功能、風險與注意。每組先預覽最多 5 筆，點擊後再展開；預設一跳，使用者要求追查時才展開第二跳。API 的 groups 欄位直接提供群組、筆數與預覽節點，手機端不需自行猜分類。

藥名消歧保持嚴格：Retinol／A 醇是 Ingredient；Tretinoin／A 酸是 DrugActiveIngredient，兩者不會因為都有「A」而合併。藥品的症狀或疾病連結必須經由 TFDA 核准適應症原文，不能從商品名或模型生成。

### 4. 證據與宣稱分層

- documented：邊具有來源與說明。
- source-linked：來源節點帶有可追溯來源。
- derived：由既有欄位與決定性規則分類。
- derived-review：實體或分類尚待確認。
- ontology-review：只表示導覽相關性，不是療效或治療宣稱。
- needs-review：舊資料只有關係、沒有 edge-level 引用；正式模式應醒目標示或隱藏。

正式圖譜解說預設由 Terra 根據回傳圖譜的節點、邊與證據摘要轉成繁中說明；節點 ID、關係 ID 與複核提醒由程式決定性附加，模型不能自行生成。TAIDE 不再是正式解說器，只保留為明確開啟的實驗 provider。兩者都不得自行新增診斷、療效、濃度或因果關係。

## 為何不會被 100 個成分限制

100 個成分是第一版驗收核心，不是查詢上限：

- 查詢服務會合併 versioned seed overlay 與現有 Neo4j 結果。
- 新資料以相同 ontology 加入新節點、別名、來源與邊，不需修改前端圖形結構。
- 後續 ingestion 流程採「自頂向下本體＋自底向上抽取」：來源匯入 → NER → 實體消歧／連結 → 關係抽取 → 證據評分 → 人工覆核 → 發布。
- 同名節點不得直接新增，必須先經 canonical ID 與 alias 對齊；本次已把 50 個既有 SkinConcern 合併投影成 47 個標準節點。

## 安全與回退

- KNOWLEDGE_GRAPH_ENABLED=0 時 API 不公開。
- KNOWLEDGE_EXPLAINER_PROVIDER 預設為 terra；設為 off 時只回傳決定性的圖譜摘要。
- 只有同時設定 KNOWLEDGE_EXPLAINER_PROVIDER=taide 與 KNOWLEDGE_TAIDE_ENABLED=1，才會呼叫實驗用 8200。
- Terra 失敗時回退 graph-fallback，不會靜默改用 TAIDE。
- 種子檔目前是 read-only overlay，沒有修改 production Neo4j。
- 停用只需關閉 feature flag；移除 overlay 可回到原本 live Neo4j 查詢。
- 在資料審核與 staging smoke test 通過前，不執行正式匯入、程序重啟或 production 切換。

## 驗證

    python scripts/validate_knowledge_seed.py
    pytest -q tests/test_knowledge_seed_repository.py tests/test_knowledge_explorer.py tests/test_knowledge_explorer_routes.py

必要驗收案例：酒糟肌、玫瑰痤瘡、痘痘、敏感肌、我有酒糟肌 都要解析到唯一標準節點；每一條回傳邊都要有 evidence_status 與有效 source_ids。


## v1.1 navigation and medicine-name layer

- Adds 12 CareNeed nodes and explicit need-to-function, need-to-ingredient and need-to-concern navigation edges.
- The seed repository traverses every edge in both directions; inverse labels are presentation metadata, not duplicated facts.
- Adds a DrugClass layer and representative non-cancelled TFDA drug products for acne and common topical medicines.
- Ambiguous product fragments such as 皮爽 resolve to candidate products through MAY_REFER_TO; they never collapse into a single active ingredient.
- Legacy hard conflicts have been removed. Conditional irritation relations remain hidden as needs-review until evidence is upgraded.
- Product recommendation is intentionally out of scope for this phase.


## v1.2 explanation provider

- Production-facing explanations default to Terra through the shared app/clients/terra_completion_client.py adapter.
- The API returns deterministic node_ids, edge_ids and evidence_statuses under citations; these values never come from model output.
- needs-review, ontology-review and derived-review notices are enforced after generation.
- Rollback is configuration-only: set KNOWLEDGE_EXPLAINER_PROVIDER=off for deterministic graph text, or explicitly opt into taide plus KNOWLEDGE_TAIDE_ENABLED=1 for the 8200 experiment.
## 2026-11-01 knowledge map JSON source

- Set KNOWLEDGE_EXPLORER_SOURCE=json to read data/knowledge_graph/v1/knowledge_map.json for knowledge explorer search, neighborhood, and explore endpoints.
- The JSON is a snapshot of the currently public reviewed BlinkKGv1 map: 36 nodes and 33 relationships. EvidenceSource nodes and legacy graph content are excluded.
- The file is loaded when the backend starts; later Neo4j changes do not update this snapshot automatically. Refresh the JSON and restart the managed backend to publish changes.
- Other knowledge-agent and product flows keep their existing data sources.
