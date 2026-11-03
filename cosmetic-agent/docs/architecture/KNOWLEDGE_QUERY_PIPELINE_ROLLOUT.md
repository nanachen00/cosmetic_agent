# 美妝知識地圖查詢管線：staging 與回滾手冊

## 範圍與隔離

此管線只提供新的 POST /api/knowledge/query。它不修改既有聊天、KnowledgeSubAgent、串流回覆或前端。兩個旗標預設皆關閉：KNOWLEDGE_GRAPH_ENABLED 與 KNOWLEDGE_QUERY_PIPELINE_ENABLED。關閉任一旗標即可隱藏新端點，不需回退聊天程式。

## 查詢順序

1. 精確名稱與別名比對；命中時直接讀取圖譜，不使用 GPU。
2. 未命中才呼叫 BGE-M3 產生 1024 維查詢向量。
3. Neo4j 只搜尋 Ingredient 與 SkinConcern，不把大量 Product 節點混入候選。
4. multilingual reranker 重排候選；reranker 未設定或失敗時 fail closed，不採用向量第一名。
5. 以固定 relation allowlist 過濾圖譜邊，並回傳來源與 evidence status。
6. Terra 只解釋過濾後資料；引用超出允許 edge ID 時改用固定模板。
7. API 預設 explain=false，讓前端先畫節點與關係；使用者需要白話說明時再非同步要求 Terra 解釋，不阻塞第一屏。

大型向量資料本身存放在 Neo4j；MI300X 的工作是在線計算 query embedding 與 rerank，不是每次重新計算全部資料。離線重建整批節點 embedding 時也應使用 GPU batch job。

## 目前實測事實（2026-10-03）

- 127.0.0.1:8002 的 BGE-M3 可回傳 1024 維向量，但目前程序設定是 CPU，不是 MI300X。
- 三組真實語意查詢的 embedding 約 22–28 ms；Neo4j 熱查約 37–40 ms，第一次連線約 412 ms。
- 向量候選仍有排序誤差，因此 reranker 是正式啟用的必要條件。
- 目前沒有可用的真實 reranker 服務；新端點因此維持關閉。
- 原本 8010 與前端未重啟、未部署、未切換。
- Jupyter 測試 kernel 仍是 redis 4.0.0、沒有 redis.asyncio；requirements-backend.txt 已要求 redis>=5,<9，而實際 8010 的 rocm_env 是 redis 8.1.0。這是環境漂移，staging 重建時必須用 requirements-backend.txt 驗證。
- 已使用 8010 的 rocm_env 對本機 Redis 完成真實 FIFO 測試：1 個執行槽、1 個排隊槽可正確交棒，釋放後 active/queue 均回到 0。

## staging 必要環境

- KNOWLEDGE_GRAPH_ENABLED=1
- KNOWLEDGE_QUERY_PIPELINE_ENABLED=1
- EMBEDDING_BASE_URL=http://mi300x-embedding-host/v1
- EMBEDDING_MODEL=bge-m3
- EMBEDDING_DIMENSION=1024
- EMBEDDING_TIMEOUT_SECONDS=2.0
- RERANKER_BASE_URL=http://mi300x-reranker-host/v1
- RERANKER_MODEL=bge-reranker-v2-m3
- RERANKER_TIMEOUT_SECONDS=2.5
- KNOWLEDGE_QUERY_ADMISSION_REDIS_URL=redis://staging-redis/0
- KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS=1
- KNOWLEDGE_QUERY_ADMISSION_KEY_PREFIX=blink:staging:knowledge:admission
- KNOWLEDGE_QUERY_MAX_RUNNING=2
- KNOWLEDGE_QUERY_MAX_QUEUED=8
- KNOWLEDGE_QUERY_QUEUE_TIMEOUT_SECONDS=0.5
- KNOWLEDGE_EXPLAINER_PROVIDER=terra

不要在 production 與 staging 共用 admission key prefix。正式環境必須設定 KNOWLEDGE_QUERY_ADMISSION_REQUIRE_REDIS=1，避免多 worker 時各自放行流量。

## 上線順序

1. 在 MI300X 部署 BGE-M3 與 bge-reranker-v2-m3 兩個獨立服務，提供 /livez、/readyz 與裝置 metadata；確認回報 ROCm/GPU，不只看模型名稱。
2. 先保持兩個知識旗標關閉，部署帶新 APP_RELEASE 的 staging API。
3. 驗證 reranker 契約、1024 維一致性、Neo4j 查詢、Redis admission 與錯誤 503。
4. 在 staging 打開旗標，測 1、2、4 workers；量 p50/p95、queue wait、event-loop lag、503 比例與取消後資源釋放。
5. 跑固定的皮膚問題/成分評測集，確認 entity linking、top-1 rerank、關係 allowlist 與零新增醫療療效。
6. 後端通過後才接前端；production 仍需 canary/rolling 與明確核准。

## 回滾

最快回滾是將 KNOWLEDGE_QUERY_PIPELINE_ENABLED 設為 0；必要時再將 KNOWLEDGE_GRAPH_ENABLED 設為 0。此操作不更動現有聊天端點。若只停用 GPU 服務，新端點會回傳 503，不會改走未經 rerank 的答案，也不會接管聊天流程。

## 驗收條件

- 精確別名完全不呼叫 embedding/reranker。
- 語意查詢一定經過 BGE-M3 與 reranker；任何必要階段缺失都 fail closed。
- relation allowlist 外的邊不回傳，Terra 引用不得越界。
- 多 worker 使用 Redis admission，過載快速 503，不阻塞 event loop。
- 前端尚未接入；production 尚未部署。
