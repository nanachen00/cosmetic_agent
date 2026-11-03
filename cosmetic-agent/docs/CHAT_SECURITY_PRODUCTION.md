# BLINK AI 聊天資安與成本防護

本文件對應目前程式內的硬性防護。前端限制只改善操作體驗，真正的身分、額度、併發與防重送判斷都在後端執行。

## 目前預設值

| 範圍 | 每分鐘 | 每小時 | 每日 |
|---|---:|---:|---:|
| 登入帳號 | 12 | 120 | 300 |
| 訪客 | 6 | 30 | 60 |
| IP | 30 | 300 | 1,000 |

另外預設限制：

- 單帳號同時只能有 1 個 AI 生成請求。
- 單則輸入最多約 2,000 Token。
- 登入帳號每日預留 Token 上限 300,000；訪客 60,000。
- 全站每日聊天上限 5,000 次、預留 Token 上限 3,000,000。
- 每次先預留輸入估算量加 400 個輸出 Token；現有模型呼叫本身另有 200～300 個輸出 Token 上限。
- 全站額度到 80% 時只記錄一次 `chat_budget_warning`；達停止線即回傳 429，不呼叫模型。
- 相同 `Idempotency-Key` 一小時內不能再次扣費；同一 key 換內容會被拒絕。

這組數值適合初期一般保養諮詢：正常人即使連續聊天也不容易碰到分鐘限制，但自動化濫用會很快被阻擋。上線一週後用正常流量的 P95／P99 調整，不要只依猜測放寬。

## Redis：正式環境必做

安裝相依套件後設定：

```dotenv
REDIS_URL=rediss://使用者:密碼@Redis主機:6379/0
CHAT_LIMIT_REQUIRE_REDIS=1
```

`CHAT_LIMIT_REQUIRE_REDIS=1` 代表 Redis 故障時聊天回 503，不會靜默退回各 Worker 自己的記憶體計數。這是付費 AI 正式環境建議值。

沒有 Redis 時仍會使用有容量上限的單程序保護，適合本機開發；它無法保證多 Worker／多主機一致，也不能當作正式完成。

建議 Redis：

- 僅允許後端網段連線並啟用 TLS。
- 使用專用帳號及最小權限，不與 Session 或工作佇列共用無上限的 eviction 設定。
- 設連線、記憶體與可用率監控；正式環境做一次兩 Worker 共用計數測試。

## 可調整的環境變數

```dotenv
CHAT_ACCOUNT_PER_MINUTE=12
CHAT_ACCOUNT_PER_HOUR=120
CHAT_ACCOUNT_PER_DAY=300
CHAT_GUEST_PER_MINUTE=6
CHAT_GUEST_PER_HOUR=30
CHAT_GUEST_PER_DAY=60
CHAT_IP_PER_MINUTE=30
CHAT_IP_PER_HOUR=300
CHAT_IP_PER_DAY=1000

# 建立新對話的獨立限制；避免繞過單一對話容量限制。
CHAT_CONVERSATION_CREATE_ACCOUNT_PER_MINUTE=5
CHAT_CONVERSATION_CREATE_ACCOUNT_PER_HOUR=20
CHAT_CONVERSATION_CREATE_ACCOUNT_PER_DAY=50
CHAT_CONVERSATION_CREATE_IP_PER_MINUTE=20
CHAT_CONVERSATION_CREATE_IP_PER_HOUR=60
CHAT_CONVERSATION_CREATE_IP_PER_DAY=200

CHAT_MAX_INPUT_TOKENS=2000
CHAT_MAX_HISTORY_TOKENS=3000
CHAT_MAX_PROFILE_TOKENS=1000
CHAT_MAX_SESSION_HISTORY_MESSAGES=20
CHAT_MAX_SESSION_HISTORY_CHARS=12000
CHAT_RESERVED_OUTPUT_TOKENS=400

CHAT_ACCOUNT_DAILY_TOKENS=300000
CHAT_GUEST_DAILY_TOKENS=60000
CHAT_GLOBAL_DAILY_REQUESTS=5000
CHAT_GLOBAL_DAILY_TOKENS=3000000
CHAT_BUDGET_WARNING_PERCENT=80

CHAT_CONCURRENCY_LEASE_SECONDS=300
CHAT_IDEMPOTENCY_TTL_SECONDS=3600
# 至少 32 bytes 隨機值；讓 Better Stack 中的帳號/IP 雜湊無法被字典反查。
CHAT_SECURITY_LOG_HMAC_SECRET=請替換成獨立隨機密鑰
OPENAI_REQUEST_TIMEOUT_SECONDS=60
OPENAI_MAX_RETRIES=1
```

若要同時用金額停止線，先依實際模型供應商價格設定估算費率。單位是 micro-USD（1 美元 = 1,000,000 micro-USD）：

```dotenv
CHAT_ESTIMATED_MICRO_USD_PER_1K_TOKENS=依實際價格填寫
CHAT_ACCOUNT_DAILY_MICRO_USD=單帳號每日停止線
CHAT_GLOBAL_DAILY_MICRO_USD=全站每日停止線
```

未填價格時，程式仍以每日 Token 和請求數做硬停止，不會用猜測價格計費。

## 訪客與對話 IDOR

訪客 ID 現在由後端簽發的 `HttpOnly`、`Secure`、`SameSite=Lax` Cookie 決定。後端忽略 body／query 中自稱的 guest ID，因此更換 `user_id` 不能讀寫別人的訪客對話。

正式環境建議另外設定一組長期密鑰：

```dotenv
CHAT_GUEST_SESSION_SECRET=至少32字元的隨機祕密
```

未設定時會從現有 `PASSWORD_RESET_SECRET` 做用途分離後使用。密鑰只能放後端環境變數，不能使用 `NEXT_PUBLIC_*`。

## Cloudflare 每 IP 操作

先保護 origin，否則攻擊者可直連 origin 並偽造 `CF-Connecting-IP`／`CF-Ray`：

1. Origin 防火牆只允許 Cloudflare 官方 IP 進入 443，或改用 Cloudflare Tunnel。
2. Cloudflare SSL 使用 Full (strict)，並考慮 Authenticated Origin Pulls。
3. Nginx 的 `set_real_ip_from` 只列 Cloudflare 官方 CIDR，`real_ip_header CF-Connecting-IP`。
4. Nginx 覆寫 `X-Real-IP`、`X-Forwarded-For`，不要保留使用者送來的值。
5. Uvicorn 只綁 localhost／Unix socket，`--forwarded-allow-ips=127.0.0.1`，不要使用 `*`。
6. 完成以上步驟後才設定 `CHAT_TRUST_PROXY_HEADERS=1`。

Cloudflare Dashboard：`Security → Security rules → Create rule → Rate limiting rules`。

聊天規則 expression：

```text
http.request.method eq "POST" and
http.request.uri.path in {
  "/api/v1/chat"
  "/api/chat/message"
  "/api/chat/message/stream"
}
```

初始值建議 `20 requests / 1 minute / IP`，動作 `Block`、期間 `1 minute`、狀態碼 `429`。API／SSE 不建議用會回 HTML 的 Managed Challenge。

建立對話另設：

```text
http.request.method eq "POST" and
http.request.uri.path eq "/api/conversations"
```

建議 `10 requests / 10 minutes / IP`，Block 10 分鐘。登入、註冊與重設密碼可另設 `5～10 requests / 10 minutes / IP`。

官方參考：[Cloudflare IP](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)、[Rate limiting rules](https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/)、[Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/)、[Nginx real IP](https://nginx.org/en/docs/http/ngx_http_realip_module.html)。

## Better Stack 與日誌

後端擋下時會記錄：路由、阻擋原因、帳號雜湊、IP 雜湊；不記錄完整聊天、Cookie 或 Session Token。email、台灣手機、Bearer、JWT 與常見 secret 也會在送出前遮蔽。

建議告警：

- `chat_budget_warning`：立即通知成本接近停止線。
- `chat_request_blocked` 且 `reason=global_tokens|global_cost`：P1。
- `reason=guard_unavailable`：P1，代表正式聊天保護不可用。
- 429 大量增加：聚合後通知，不要每筆各發一封。

Cloudflare 在邊緣擋掉的請求不會到後端，所以 Better Stack 後端 log 看不到；應看 Cloudflare Security Events，或用 Logpush 傳到集中日誌。

## 文字內容仍要注意

- React 目前以純文字輸出，會自動跳脫 XSS。未來加入 Markdown 時不要開 raw HTML，必須過濾連結協定與 HTML，並為外部連結加安全屬性。
- SQL 必須全部參數化；表名、排序欄位等不能綁參數的部分只能 allowlist。限流不能取代 SQL Injection 稽核。
- Prompt Injection 沒有可靠的「關鍵字過濾」解法。真正邊界是模型上下文不放祕密／其他人資料，工具採 allowlist，且每次工具呼叫在後端重新驗證 owner 與權限。
- 目前 Session 內歷史最多 20 則／12,000 字元，GetZep context 約 3,000 Token，避免歷史無限帶入模型。
- 個資核心策略是聊天正文不進監控、HTTPS、保存期限與刪除能力；regex 只能降低誤記錄，不能當完整 DLP。

## 部署前驗收

1. 設定 Redis 並讓 `TEST_REDIS_URL` 測試不是 skipped。
2. 兩個後端 Worker 共用同一 Redis；第 N+1 次必須被另一 Worker 阻擋。
3. 帳號 A 不能讀、改、刪帳號 B 的 conversation。
4. 訪客 Cookie 缺失、竄改、過期與 body 換 ID 全部被拒絕。
5. 同一 Idempotency-Key 並發兩次，只能啟動一次模型。
6. 429 包含 `Retry-After`；Redis 故障且 require 模式回 503。
7. 直連 origin 必須失敗；偽造 Cloudflare header 不能改 IP。
8. Better Stack 實際收到一次測試告警，內容不含訊息正文與 Token。
9. 前端 typecheck、production build、登入與聊天核心流程全部通過。

## 單一對話容量限制

後端是唯一判定來源；前端的停用輸入框只提供操作提示，不能當成安全控制。

預設值：

- 每個對話最多 40 則使用者訊息。
- 每個對話最多估算 30,000 Token（包含使用者與助理訊息）。
- 使用量達 80% 時顯示即將額滿提示。
- 第 40 則訊息可完成；完成後該對話鎖定輸入。下一次請求由後端回傳 HTTP 409、code=conversation_limit_reached。
- 額滿後保留完整歷史，只能閱讀；使用者應按「開新對話」繼續。

正式環境可調整：

- CHAT_CONVERSATION_MAX_USER_MESSAGES
- CHAT_CONVERSATION_MAX_TOKENS
- CHAT_CONVERSATION_WARNING_PERCENT

部署後驗收：

1. 將測試環境上限暫設為 5，確認第 5 則完成後輸入框、快捷回覆與送出按鈕全部停用。
2. 重新整理頁面後仍保持鎖定，不能只依賴前端記憶體。
3. 直接呼叫三個聊天端點時，第 6 次都收到 409 與 conversation_limit 狀態。
4. 點「開新對話」後可以正常輸入，舊對話仍可閱讀。
5. 確認額度警告與阻擋事件沒有記錄完整聊天內容、Cookie 或 Token。
