# BLINK AI 資料責任邊界

本文件描述開發環境啟用 `AUTH_STORAGE_BACKEND=postgres` 後的唯一資料來源。

## PostgreSQL：會員與個人業務資料

- `users`：不可變的內部 UUID，所有登入方式最後都回到此 ID。
- `user_emails`：Email 與驗證狀態；同一 verified Email 不得建立第二個會員。
- `auth_identities`：password、Google、LINE 身分與同一 UUID 的關聯。
- `auth_sessions`：登入 Session 與登入方式。
- `user_profiles`：基本資料。
- `daily_checkins`：每日簽到，`user_id + checkin_date` 唯一。
- `quiz_attempts`、`quiz_answers`：問卷結果。
- `skin_records`：每日膚況紀錄。
- `conversations`、`external_memory_links`：前端 conversation ID、Zep thread/user 與 UUID 的對照。
- `password_reset_challenges`：只存驗證碼雜湊、時效、剩餘嘗試次數與使用時間。

## Zep：對話內容與長期記憶

- Zep user ID 固定使用 PostgreSQL `users.id` UUID。
- thread ID 固定由 `UUID + client_conversation_id` 雜湊產生。
- PostgreSQL 只存對照與中繼資料，不複製 Zep 訊息正文。

## Neo4j：知識圖譜

保留商品、成分、膚況、過敏／禁忌、商品分類、推薦與法規關係。會員密碼、Session、Email、OAuth 身分與個人 Profile 不再新增至 Neo4j。

舊會員節點先保留作回復用途；未經資料合併預覽與人工確認，不刪除。

## MongoDB：過渡期舊資料

舊 collection 暫時唯讀保留。新寫入在 PostgreSQL 完成後，才另行安排資料搬移或封存；目前不直接刪除。

