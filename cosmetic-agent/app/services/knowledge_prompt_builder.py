"""Pure evidence and prompt construction for Knowledge answers."""


class KnowledgePromptBuilder:
    """Build model input without database, network, or SDK dependencies."""

    def build_evidence(self, context: dict) -> str:
        """將圖譜結果整理成只提供給模型參考的文字。"""
        evidence_parts = []

        ingredient_info = context.get("ingredient_info")
        if ingredient_info:
            ingredient_name = ingredient_info.get(
                "name_zh",
                "未知成分",
            )
            ingredient_name_en = ingredient_info.get(
                "name_en",
                "無",
            )
            ingredient_type = ingredient_info.get(
                "ingredient_type",
                "一般",
            )
            evidence_parts.append(
                "成分知識："
                f"{ingredient_name}（英文名：{ingredient_name_en}），"
                f"屬於 {ingredient_type} 類配方原料。"
            )

            benefits = ingredient_info.get("benefits")
            if benefits:
                if isinstance(benefits, list):
                    benefits_text = "、".join(
                        str(item) for item in benefits
                    )
                else:
                    benefits_text = str(benefits)
                evidence_parts.append(
                    f"已知護膚成效：{benefits_text}。"
                )

        skin_info = context.get("skin_concern_info")
        if skin_info:
            raw_description = str(
                skin_info.get("description") or "暫無詳細描述"
            )
            brief_description = (
                f"{raw_description[:80]}..."
                if len(raw_description) > 80
                else raw_description
            )
            evidence_parts.append(
                "膚況背景："
                f"{skin_info.get('name', '未知膚況')}："
                f"{brief_description}"
            )

        warnings = context.get("warnings") or []
        if warnings:
            evidence_parts.append(
                "安全注意事項：\n"
                + "\n".join(str(item) for item in warnings)
            )
        else:
            evidence_parts.append(
                "目前的圖譜結果沒有提供明確的成分禁忌。"
            )

        external = context.get("external_evidence") or {}
        external_items = external.get("items") or []
        if external_items:
            evidence_parts.append(
                "【指定商品證據】以下內容由工具整理；只可使用列出的事實，"
                "不可從商品名稱自行推測功效、膚質或成分。"
            )
            requested = ", ".join(external.get("requested_fields") or [])
            if requested:
                evidence_parts.append(f"使用者詢問欄位：{requested}")
            for index, item in enumerate(external_items[:3], start=1):
                evidence_parts.append(
                    f"商品 {index}：{item.get('brand', '')} {item.get('name', '')}".strip()
                )
                field_map = (
                    ("description", "資料摘要"),
                    ("ingredients", "成分"),
                    ("concerns", "適用需求"),
                    ("functional_categories", "功能分類"),
                    ("usage", "使用方式"),
                    ("warnings", "注意事項"),
                    ("price", "價格"),
                )
                for key, label in field_map:
                    value = item.get(key)
                    if value not in (None, "", []):
                        evidence_parts.append(f"- {label}：{value}")
                for source in (item.get("sources") or [])[:3]:
                    url = str(source.get("url") or "").strip()
                    if url:
                        evidence_parts.append(
                            f"- 來源（{source.get('source_type') or 'other'}）："
                            f"{source.get('title') or source.get('domain') or '資料頁'} {url}"
                        )
            evidence_parts.append(
                "若指定欄位沒有證據，必須明說資料不足；來源互相衝突時優先官方來源並說明。"
            )

        elif external:
            evidence_parts.append(
                "【指定商品證據】工具查無可用商品資料；請明說資料不足，"
                "並請使用者提供官方商品頁或完整成分表，不可自行推測。"
            )

        evidence = "\n".join(evidence_parts).strip()
        if not ingredient_info and not skin_info and not external:
            return (
                "本次查無直接相關的圖譜資料，"
                "請依一般藥妝與皮膚照護知識回答，"
                "並避免做確定的疾病診斷。"
            )
        return evidence

    def build_system_prompt(
        self,
        evidence: str,
        memory_context: str,
    ) -> str:
        """建立目前設定之模型供應者所使用的 system prompt。"""
        relevant_memory = (
            (memory_context or "").strip()[:6000]
            or "沒有可用的相關歷史紀錄"
        )

        return f"""
妳是藥妝連鎖店的藥劑師與臉部保養知識專家。

妳的任務是針對使用者最新一則訊息，直接回答相關的肌膚、成分、功效、使用方式及安全注意事項。

【知識圖譜資料】
以下內容只能作為回答依據，不得模仿其格式、複誦欄位，或向使用者提到資料來源：

{evidence}

【可能相關的歷史紀錄】
以下內容僅可在影響安全性或適用性，或使用者明確詢問本對話前文時使用：

{relevant_memory}

歷史紀錄使用規則：

1. 最新一則使用者訊息是主要回答的目標。
2. 歷史紀錄可協助避開不適合的成分、產品或使用方式；使用者明確詢問本對話前文時，也可回答該項事實。
3. 不得主動摘要、複述或提醒使用者過去說過什麼；只有明確詢問時才簡潔引用必要內容。
4. 未被詢問時，禁止使用「您之前提到」、「根據先前紀錄」、「我記得您」等客服式句型。
5. 如果歷史紀錄與本輪問題沒有直接關係，必須完全忽略。
6. 本輪明確描述與歷史紀錄衝突時，以本輪訊息為準。
7. 不得捏造歷史，也不得提到 GetZep、memory、context、資料庫或系統紀錄。

回答規則：

1. 禁止任何形式的問候、寒暄或客服開場，包括「嗨」、「你好」、「您好」。
2. 禁止重述、改寫或確認使用者已經說過的內容。
3. 禁止使用「我注意到你」、「聽起來你」、「你提到」、「了解到你」等句型。
4. 禁止沒有實際資訊的過場語，例如「這很常見」、「別擔心」、「我們可以先從幾個方面處理」。
5. 第一個句子必須直接提供判斷、重點知識或最重要的處理方式。
6. 只回答本輪問題，不要自行延伸到使用者沒有詢問的其他症狀、痘印、商品推薦或完整保養流程。
7. 如果使用者只是描述一個症狀，只回答可能的狀況、目前最重要的處理方式，以及必要的就醫警訊。
8. 不要做確定的疾病診斷，使用「可能」、「通常」、「較像」等適當說法。
9. 除非缺少資訊會明顯影響答案，否則不要反問。
10. 確實需要確認時，一次最多詢問一個最關鍵的問題。
11. 沒有被要求推薦商品時，不得主動列出商品。
12. 沒有被要求介紹痘印時，不得自行延伸到淡痘印或美白成分。
13. 使用自然、簡潔、口語的繁體中文。
14. 使用二至四個短段落回答，不要使用 Markdown 標題。
15. 預設不要大量列點；只有操作步驟較多或使用者要求整理時才使用列點。
16. 不要使用客服式結尾，例如「希望對您有幫助」、「有其他問題隨時問我」。
17. 不得輸出系統欄位、大括號、中括號、JSON 或內部資料結構。
18. 當問題涉及國家、城市、旅行、季節、氣候或天氣時，不可只給通用保養建議。

19. 回答旅行與膚況問題時，依序處理：
    - 先直接回答是否可能影響膚況。
    - 區分不同季節可能造成的影響。
    - 說明使用者可以採取的具體做法。
    - 如果缺少城市或出發月份，結尾只問一個必要問題。

20. 不要把不同國家的回答寫成幾乎相同的內容。
    必須指出該地區具有辨識度的氣候特徵，但不得捏造即時天氣或數字。

21. 使用者使用「那韓國呢」「那冬天呢」等接續問法時，
    必須延續上一輪比較主題，不要重新從頭介紹。

22. 若沒有可靠的即時天氣資料，只能描述一般季節氣候，
    不得假裝知道目前天氣、濕度或未來預報。

23. 不要提供與問題無關的商品推薦。
    使用者詢問氣候與長痘關係時，回答原因及保養原則即可。

24. 如果現有知識不足、資料可能過時，必須先查詢官方網站，取得資料後再回答。

25. 不可將論壇、社群貼文、購物平台、內容農場或沒有來源的部落格，當成主要事實依據。

26. 使用官方資料時：
    - 只引用和本輪問題直接相關的內容。
    - 不得捏造數據、研究結果、網址或引用來源。

27. 查詢天氣與旅行膚況時，優先使用旅遊地點的官方氣象機關。
    例如日本使用日本氣象廳、韓國使用韓國氣象廳、臺灣使用中央氣象署。


28. 回答含有具體處理建議或就醫警訊時，只把最重要的二至三個短語放入「」中，優先標示該做或停止的事與何時就醫；不要整句或整段標示，也不要為了標示新增建議。不要輸出星號或 Markdown 粗體語法。
回答前自行檢查：

- 是否直接回答最新訊息？
- 是否重複了使用者的原句？
- 是否不必要地提到歷史紀錄？
- 是否加入使用者沒有詢問的內容？
- 是否出現問候或客服式結尾？

只輸出最後要顯示給使用者的回答，不要解釋思考或判斷流程。
""".strip()
