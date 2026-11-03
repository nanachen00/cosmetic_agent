# 本機複現證據：被模擬或略過的模組

產生：2026-10-28 17:14；本機 tomod：p-sys1-bundle-20261028-r2.zip ＋ `run_local_psys1.sh sync-dev`（experiments/p-sys1）；模型檔：Llama-3.2-3B-Instruct-Q4_K_M.gguf

## 元件

|類別|元件|本機實作 / VM 對應|
|--|--|--|
|真實|backend|cosmetic-agent (uvicorn api.api_server:app :8010)|
|真實|taide_gateway|ops/runtime/taide_vllm_gateway.py :8000/:8003|
|真實|taide_router|taide_router_8001.py :8001|
|真實|postgresql|PostgreSQL 14.24 (EDB Windows binaries) :5432 — VM: Ubuntu apt PostgreSQL 14|
|真實|redis|Redis 6.2.24 (redis-windows MSYS2 build) :6389 — VM: Ubuntu apt Redis 6|
|真實|p_sys1|ops/p-sys1 from experiments/p-sys1 (bundle + run_local_psys1.sh sync-dev); only paths and LOCAL flags differ|
|模擬|taide_12b|Llama-3.2-3B-Instruct Q4_K_M on llama.cpp b10819 CUDA :8200, served as taide-12b — VM: TAIDE Gemma-3 12B bf16 on vLLM 0.8.5 ROCm|
|模擬|openai_terra|sim/openai_sim.py :8090 (OPENAI_BASE_URL) -> same llama; Terra is never called — VM: OpenAI gpt-5.6-terra|
|模擬|zep_cloud|sim/zep_sim.py :8091 (ZEP_API_URL), in-memory, no memory extraction — VM: Zep Cloud|
|模擬|embedding_bge_m3|sim/embedding_sim.py :8002, SHA-256 vectors — VM: BGE-M3 sentence-transformers|
|模擬|sys1_encoder|np-tiny-local: numpy wiring-only head on CPU, serve_sys1.py :8232, listed only in runtime/local-registry.json — VM: owner-approved candidates (mmbert-ft, mdeberta-ft, xlmr-ft) trained on MI300X|
|略過|neo4j_aura|NEO4J_* empty: product/knowledge graph retrieval unavailable — VM: Neo4j Aura (10,735 nodes)|
|略過|tavily_firecrawl|keys empty|
|略過|smtp|empty: no mail|
|略過|oauth_google_line|empty|
|略過|better_stack_otel|disabled|
|略過|cloudflare_nginx|not part of the local replica|
|略過|skin_observation_catalog|needs Neo4j; not rebuilt|
|未覆蓋|zep_writes|guest sessions skip remote long-term memory by design (agents/root/orchestrator.py, '訪客工作階段略過遠端長期記憶'); smoke and S3 are guest-only, so user.add / thread.create / add_messages never ran — member-path smoke is a later round|
|未覆蓋|product_turns|need Neo4j (skipped): smoke 商品/追問 and 3 of 16 S3 turns fail|
|未覆蓋|sys1_accuracy|np-tiny-local is wiring only: S3 agreement/safety numbers are not candidate results|

## 模擬服務實際被呼叫的次數

|模擬|端點 / 操作|要求的模型|次數|
|--|--|--|--|
|embedding|/v1/embeddings||7|
|openai|/v1/responses|gpt-5.6-terra|19|
|openai|/v1/chat/completions|gpt-5.6-terra|5|
|zep|thread.get:404||39|
|zep|user.get:404||30|
|zep|user.patch:404||9|

S3 本機結果：`s3-20261028/SUMMARY.md`（Sys1 = np-tiny-local，只驗證接線；一致率與安全差異不是候選結果）

上 VM 後，「模擬」、「略過」與「未覆蓋」列出的每一項都必須以真實服務重新驗證；本機結果不代表延遲與回答品質。
