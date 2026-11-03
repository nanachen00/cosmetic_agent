# 歷史 Smoke 證據（不是本版執行結果）

# 會員模式建立時的 Smoke 與當時修改界線

來源：20260925-071550-641742.tar.gz（不是過去找不到8010的那次preflight）。

- 本輪退出碼2，清理/正式保護/vLLM還原通過；不代表整個Smoke通過。
- 6筆合法guest正向聊天HTTP200；匿名控制401，並非所有請求未授權。
- 會員資料載入的提示出現在guest的Profile階段，正是新增固定會員模式的理由。
- :8200 product_search 已進dispatch，13.676秒；:8000 product_search 33.442秒但停在澄清。
- 兩個skin_consult已產生知識回答，仍有schema/截斷/工具policy問題，不能當完整成功。
- 5次schema失敗、2次截斷、6次backend subprocess阻擋；不應藉加入會員掩蓋。
- 沒有正式HTTP批次，所以本輪沒有有效的多VU效能比較。
- 已觀察到的Neo4j DNS阻擋沒有在這次結果中再次出現；不宣稱未來所有routing節點都已允許。

該次會員版只補齊身分測試、必要Shadow scope與diagnostics，保留模型token預算、prompt與schema判定。不能保證原模型的意圖JSON在新一輪必然通過。
