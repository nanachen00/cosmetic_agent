# 效能、併發與監控改善報告

更新日期：2026-09-25

## 結論

本輪已確認原本的 31.5 秒商品查詢不是 Neo4j 本身持續變慢，而是舊流程的逐項掃描、重複模型呼叫及排隊共同造成。商品向量查詢、皮膚諮詢與皮膚紀錄都已加入分段計時，並移除兩段可證明不必要的 Terra 呼叫。20 人同時請求的成功率由 60% 提升到 100%，但單張 GPU 下的模型生成仍是主要延遲來源。

## 實測數據

### 商品檢索

| 場景 | 結果 |
|---|---:|
| 冷啟動 embedding | 0.767 秒 |
| 冷啟動 Neo4j vector search | 0.281 秒 |
| 冷啟動檢索合計 | 1.05 秒 |
| 暖機 embedding | 約 0.022 秒 |
| 暖機 Neo4j vector search | 約 0.032 秒 |
| 暖機檢索合計 | 約 0.055 秒 |
| 完整商品流程 | 0.297 秒（首次）、0.067～0.068 秒（暖機） |

因此 Neo4j 已不是先前 31.5 秒的主要瓶頸。現行商品路徑直接使用資料庫證據組裝結果，不再先用 TAIDE 做成分抽取或文案重寫。

### 皮膚諮詢

| 版本 | 總時間 | 主要差異 |
|---|---:|---|
| 修改前 | 12.297 秒 | Terra 意圖判斷 3.541 秒 + Neo4j + Terra 最終生成 |
| 修改後 | 8.466 秒 | 明確 skin_consult 直接路由，意圖判斷降為 0 ms（reused） |

改善約 31.2%。修改後該次 Neo4j 為 0.810 秒，Terra 最終答案生成為 7.394 秒，證明剩餘主要瓶頸是語言模型生成，不是知識庫。

### 皮膚紀錄

| 版本 | 總時間 | 分段 |
|---|---:|---|
| 修改前 | 8.420 秒 | message_extract 2.968 秒；concern_assessment 5.222 秒 |
| 修改後 | 3.117 秒 | message_extract 2.900 秒；不再呼叫多餘的 concern_assessment |

改善約 63.0%。第一次抽取已得到乾燥、泛紅、程度 2 與臉頰資訊；當只需確認左／右臉頰時，現在改用固定文字和原有按鈕，不再花約 5 秒請模型生成同一個追問。非標準、無法分類的膚況仍保留模型判讀。正式重啟後再經 8010 串流 API 實測為 HTTP 200、status 0.825 秒、TTFT 4.347 秒、總計 4.348 秒；相較先前 API 9.561 秒約快 54.5%。

## 併發測試

目前保護參數為：

- CHAT_MAX_RUNNING=4
- CHAT_MAX_QUEUED=16
- CHAT_QUEUE_TIMEOUT_SECONDS=45

| 同時使用者 | 成功率 | p50 | p95 | 備註 |
|---:|---:|---:|---:|---|
| 5 | 5/5 | 4.730 秒 | 4.832 秒 | 全部成功 |
| 10 | 10/10 | 6.660 秒 | 8.096 秒 | 全部成功 |
| 20（舊 2+8） | 12/20 | 13.673 秒 | 15.490 秒 | 8 個 503 busy |
| 20（新 4+16） | 20/20 | 14.347 秒 | 16.051 秒 | wall 16.209 秒，全部成功 |

這項調整改善的是「不掉請求」，不是讓 GPU 生成速度翻倍。20 人時仍需排隊，因此下一個真正的大改善點是支援 ROCm continuous batching 的正式推理服務、縮短輸出 token，以及讓更多流程真正串流。

## 串流狀態

- skin_consult：有真實 token delta 串流。
- 商品、一般問答及部分皮膚紀錄：多數仍在最後一次送出單一 delta，前端雖使用串流介面，但後端未逐 token 回傳。
- 單次商品路徑實測：status 約 1.696 秒、第一個 delta 約 2.660 秒、總計約 2.661 秒。

## GPU 與監控

ROCm 顯示 GPU 0 有三個主要記憶體使用量，約 57.1 GiB、27.2 GiB、3.0 GiB。容器無法讀取主機 PID 的 /proc，因此只能依服務與記憶體型態推測為：8200 實驗 vLLM、8000 TAIDE、8002 embedding。沒有經基礎設施管理員確認前，不停止 8200。

CU occupancy 在閒置時為 0 是正常的：模型仍可佔用 VRAM，但當下沒有做矩陣運算。AI-Stack GPU 圖表空白則仍屬監控基礎設施問題，需啟用 AMD ROCm exporter、授權 /dev/kfd 與 /dev/dri、讓 Prometheus 抓取正確 endpoint，並將 metric/label 對應到 goodagents。這部分尚未由應用程式碼修復。

應用層已提供：

- /metrics：chat active、queued、overload 以及 pipeline stage histogram。
- 8010 日誌：每段 pipeline 的 duration_ms 與 status。
- Better Stack：目前只送 WARNING 以上，正常 INFO 計時不會全部上傳，避免增加成本與雜訊。

## 已知未完成項目

1. 8200 仍為 EXPERIMENTAL / UNMANAGED，未納入三支正式腳本，也未停止。
2. AI-Stack 的 AMD GPU exporter/Prometheus 對接需要平台管理權限。
3. Neo4j 的 db.index.vector.queryNodes 已收到棄用警告，之後應改為 SEARCH。
4. 商品資料缺少 review_score、rating 時會用 0.5，搜尋可用但排序訊號較弱。
5. 壓測後建立新對話命中 429；這是安全限流正常運作，本輪未為了測試關閉保護。
6. 單 GPU 下 20 人延遲仍高；併發成功率已改善，但推理吞吐仍需 continuous batching 才能有本質提升。

## 最終驗證

- 後端：281 passed、1 skipped、11 subtests passed。
- Python compileall：通過。
- Backend git diff --check：通過。
- 前端 TypeScript：通過。
- Next.js production build：通過，20 個頁面路由。
- Frontend git diff --check：通過。
- start/stop/status/restart-production.sh：bash -n 全部通過。

## Production restart validation — 2026-09-25

- General chat: 13.207 s (intent 6.502 s; pain/gain 4.508 s).
- Product search: 5.628 s (intent 3.110 s; vector search 0.930 s; Neo4j enrichment 0.172 s).
- SSE knowledge answer: TTFT 0.799 s; total 12.985 s (ingredient extraction 4.680 s; Terra generation 6.995 s).
- Product search at concurrency 5: 5/5 HTTP 200; median 12.751 s; p95/max 15.999 s.
- Concurrency delay was observed before admission control in agent_session and conversation_limit (about 6.7–7.7 s for some requests), while queue_wait stayed at 0 ms. Move admission control earlier before claiming queue latency is solved.

