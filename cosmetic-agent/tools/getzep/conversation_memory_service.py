"""
Conversation Memory Service
===========================

負責協調每一輪對話的 GetZep 記憶流程：

1. 先把本輪使用者訊息寫入 GetZep
2. 取得 GetZep 根據最新訊息整理出的相關歷史
3. 合併 Profile 長期偏好與 GetZep context
4. 建立可以直接交給 AI Agent 的 memory prompt
5. AI 回答完成後，把 assistant 訊息寫回 GetZep

這個 Service 不負責：

- 建立 GetZep Client
- 建立 User / Thread
- 直接操作 GetZep SDK
- 產生最終 AI 回答
- 商品搜尋或排序

低階 GetZep 操作仍由 ZepMemoryAdapter 負責。
"""

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Protocol


# =========================================================
# Adapter Interface
# =========================================================

class ConversationMemoryAdapterProtocol(
    Protocol,
):
    """
    ConversationMemoryService 需要的 Adapter 介面。

    ZepMemoryAdapter 必須提供：

    - user_id
    - thread_id
    - to_summary()
    - get_context_block()
    - add_user_message()
    - add_assistant_message()
    """

    user_id: str
    thread_id: str

    def to_summary(
        self,
    ) -> str:
        ...

    def get_context_block(
        self,
    ) -> str:
        ...

    def add_user_message(
        self,
        content: str,
    ) -> None:
        ...

    def add_assistant_message(
        self,
        content: str,
    ) -> None:
        ...


# =========================================================
# Memory Context
# =========================================================

@dataclass(frozen=True)
class ConversationMemoryContext:
    """
    本輪對話取得的記憶資料。

    profile_summary：
        從 Zep User metadata 或 Neo4j Profile
        整理出的長期偏好。

    zep_context：
        GetZep 根據最新對話整理出的歷史 context。

    prompt：
        已組裝完成、可以直接交給 AI Agent 的文字。
    """

    user_id: str

    thread_id: str

    user_message: str

    profile_summary: str

    zep_context: str

    prompt: str

    user_message_saved: bool

    context_loaded: bool


# =========================================================
# Conversation Memory Service
# =========================================================

class ConversationMemoryService:
    """
    每一輪對話的 GetZep 記憶協調服務。
    """

    def __init__(
        self,
        adapter:
            ConversationMemoryAdapterProtocol,
        max_profile_chars: int = 2000,
        max_context_chars: int = 6000,
        max_profile_tokens: Optional[int] = None,
        max_context_tokens: Optional[int] = None,
    ):
        self.adapter = adapter

        self.max_profile_chars = max(
            500,
            max_profile_chars,
        )

        self.max_context_chars = max(
            1000,
            max_context_chars,
        )

        self.max_profile_tokens = max(
            256,
            max_profile_tokens
            if max_profile_tokens is not None
            else int(os.getenv("CHAT_MAX_PROFILE_TOKENS", "1000")),
        )

        self.max_context_tokens = max(
            512,
            max_context_tokens
            if max_context_tokens is not None
            else int(os.getenv("CHAT_MAX_HISTORY_TOKENS", "3000")),
        )


    # -----------------------------------------------------
    # Logging
    # -----------------------------------------------------

    def _log(
        self,
        stage: str,
        message: str,
    ) -> None:
        print(
            "[ConversationMemoryService/"
            f"{stage}] "
            f"{message}"
        )


    def _user_ref(self) -> str:
        return hashlib.sha256(
            str(self.adapter.user_id).encode("utf-8"),
        ).hexdigest()[:16]


    # -----------------------------------------------------
    # Text Utilities
    # -----------------------------------------------------

    def _normalize_text(
        self,
        value: Optional[str],
    ) -> str:
        """
        統一清理文字。

        - None 轉空字串
        - 去除前後空白
        - 統一換行
        - 避免過多連續空行
        """

        if value is None:
            return ""

        text = str(
            value
        ).replace(
            "\r\n",
            "\n",
        ).replace(
            "\r",
            "\n",
        ).strip()

        while "\n\n\n" in text:
            text = text.replace(
                "\n\n\n",
                "\n\n",
            )

        return text


    def _truncate_text(
        self,
        text: str,
        max_chars: int,
    ) -> str:
        """
        限制 context 長度，避免 Prompt 太大。

        保留開頭與結尾：
        - 開頭通常包含使用者摘要
        - 結尾通常包含最近相關記憶
        """

        normalized = (
            self._normalize_text(
                text
            )
        )

        if (
            not normalized
            or len(normalized)
            <= max_chars
        ):
            return normalized

        separator = (
            "\n\n"
            "……中間部分已省略……"
            "\n\n"
        )

        remaining_chars = (
            max_chars
            - len(separator)
        )

        head_chars = int(
            remaining_chars
            * 0.4
        )

        tail_chars = (
            remaining_chars
            - head_chars
        )

        return (
            normalized[
                :head_chars
            ]
            + separator
            + normalized[
                -tail_chars:
            ]
        )


    def _truncate_tokens(
        self,
        text: str,
        max_tokens: int,
    ) -> str:
        """Apply a conservative UTF-8 byte budget for mixed CJK prompts."""
        normalized = self._normalize_text(text)
        max_bytes = max_tokens * 3
        raw = normalized.encode("utf-8")
        if len(raw) <= max_bytes:
            return normalized

        separator = "\n\n……中間部分已省略……\n\n"
        separator_bytes = separator.encode("utf-8")
        remaining = max(1, max_bytes - len(separator_bytes))
        head_bytes = int(remaining * 0.4)
        tail_bytes = remaining - head_bytes
        head = raw[:head_bytes].decode("utf-8", errors="ignore")
        tail = raw[-tail_bytes:].decode("utf-8", errors="ignore")
        return head + separator + tail


    # -----------------------------------------------------
    # Prompt Builder
    # -----------------------------------------------------

    def build_prompt(
        self,
        user_message: str,
        profile_summary: str = "",
        zep_context: str = "",
    ) -> str:
        """
        組成可以直接傳給 Agent 的記憶 Prompt。

        規則：

        1. 歷史只作為參考
        2. 本輪明確描述優先
        3. 不可捏造歷史
        4. 有關聯時自然使用
        5. 不要向使用者提到 GetZep、資料庫或 context
        """

        current_message = (
            self._normalize_text(
                user_message
            )
        )

        profile = (
            self._truncate_tokens(
                self._truncate_text(
                    profile_summary,
                    self.max_profile_chars,
                ),
                self.max_profile_tokens,
            )
        )

        history = (
            self._truncate_tokens(
                self._truncate_text(
                    zep_context,
                    self.max_context_chars,
                ),
                self.max_context_tokens,
            )
        )

        sections: list[str] = []

        sections.append(
            "【記憶使用規則】\n"
            "以下資料只供本輪回答參考。\n"
            "1. 只使用與本輪問題有關的歷史資訊。\n"
            "2. 如果本輪描述與過去紀錄衝突，以本輪描述為準。\n"
            "3. 不可捏造歷史中沒有出現的商品、偏好或使用經驗。\n"
            "4. 不要向使用者提到 GetZep、資料庫、記憶系統或 context。\n"
            "5. 如果歷史與本輪問題無關，不需要刻意引用。\n"
            "6. 若使用者過去明確表示不喜歡某商品、質地或成分，"
            "推薦時應避免重複推薦，或清楚說明差異。\n"
            "7. 若使用者曾購買某商品，回答時可以自然承接該使用經驗。"
        )

        if profile:
            sections.append(
                "【使用者基本資料與長期偏好】\n"
                f"{profile}"
            )

        if history:
            sections.append(
                "【與目前對話相關的歷史紀錄】\n"
                f"{history}"
            )

        if current_message:
            sections.append(
                "【本輪使用者問題】\n"
                f"{current_message}"
            )

        return "\n\n".join(
            sections
        )


    # -----------------------------------------------------
    # Begin Turn
    # -----------------------------------------------------

    def begin_turn(
        self,
        user_message: str,
        profile_summary:
            Optional[str] = None,
    ) -> ConversationMemoryContext:
        """
        開始一輪對話。

        執行順序：

        1. 正規化本輪 user message
        2. 先寫入 GetZep
        3. 取得 Profile summary
        4. 取得 GetZep context
        5. 組成 Agent prompt

        即使 GetZep 暫時失敗，
        仍會回傳可用的空 context，
        不讓整個聊天功能中斷。
        """

        normalized_message = (
            self._normalize_text(
                user_message
            )
        )

        if not normalized_message:
            raise ValueError(
                "user_message 不可為空"
            )

        user_message_saved = False

        context_loaded = False

        # -------------------------------------------------
        # 先保存本輪使用者訊息
        # -------------------------------------------------

        try:
            self.adapter.add_user_message(
                normalized_message
            )

            user_message_saved = True

            self._log(
                "BEGIN",
                "已寫入本輪 user message，"
                f"account_hash={self._user_ref()}",
            )

        except Exception as error:
            self._log(
                "WARNING",
                "寫入 user message 失敗，"
                "本輪將降級為沒有即時 GetZep context："
                f"{type(error).__name__}",
            )

        # -------------------------------------------------
        # 取得 Profile summary
        # -------------------------------------------------

        if profile_summary is None:
            try:
                profile = (
                    self.adapter
                    .to_summary()
                )

            except Exception as error:
                self._log(
                    "WARNING",
                    "取得 Profile summary 失敗："
                    f"{type(error).__name__}",
                )

                profile = ""

        else:
            profile = profile_summary

        profile = (
            self._truncate_tokens(
                self._truncate_text(
                    profile,
                    self.max_profile_chars,
                ),
                self.max_profile_tokens,
            )
        )

        # -------------------------------------------------
        # 取得 GetZep context
        # -------------------------------------------------

        try:
            zep_context = (
                self.adapter
                .get_context_block()
            )

            zep_context = (
                self._truncate_tokens(
                    self._truncate_text(
                        zep_context,
                        self.max_context_chars,
                    ),
                    self.max_context_tokens,
                )
            )

            context_loaded = bool(
                zep_context
            )

            self._log(
                "CONTEXT",
                "GetZep context 讀取完成，"
                f"chars={len(zep_context)}",
            )

        except Exception as error:
            self._log(
                "WARNING",
                "讀取 GetZep context 失敗，"
                "本輪改用 Profile 與 Session history："
                f"{type(error).__name__}",
            )

            zep_context = ""

        # -------------------------------------------------
        # 組成 Agent Prompt
        # -------------------------------------------------

        prompt = self.build_prompt(
            user_message=
                normalized_message,
            profile_summary=
                profile,
            zep_context=
                zep_context,
        )

        return ConversationMemoryContext(
            user_id=
                self.adapter.user_id,

            thread_id=
                self.adapter.thread_id,

            user_message=
                normalized_message,

            profile_summary=
                profile,

            zep_context=
                zep_context,

            prompt=
                prompt,

            user_message_saved=
                user_message_saved,

            context_loaded=
                context_loaded,
        )


    # -----------------------------------------------------
    # Finish Turn
    # -----------------------------------------------------

    def finish_turn(
        self,
        assistant_message: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        AI 回答完成後寫入 GetZep。

        成功回傳 True。
        寫入失敗回傳 False，但不阻止前端收到回答。
        """

        normalized_message = (
            self._normalize_text(
                assistant_message
            )
        )

        if not normalized_message:
            self._log(
                "WARNING",
                "assistant message 為空，"
                "略過 GetZep 寫入",
            )

            return False

        try:
            self.adapter.add_assistant_message(
                normalized_message,
                metadata=metadata,
            )

            self._log(
                "FINISH",
                "已寫入 assistant message，"
                f"account_hash={self._user_ref()}",
            )

            return True

        except Exception as error:
            self._log(
                "WARNING",
                "寫入 assistant message 失敗："
                f"{type(error).__name__}",
            )

            return False


    # -----------------------------------------------------
    # Failure Turn
    # -----------------------------------------------------

    def finish_error_turn(
        self,
        public_error_message: str,
    ) -> bool:
        """
        如果 Agent 發生錯誤，但前端有顯示一段公開錯誤訊息，
        可以選擇把該訊息寫入 GetZep。

        不要把 traceback、API key 或內部錯誤直接寫入 GetZep。
        """

        normalized_message = (
            self._normalize_text(
                public_error_message
            )
        )

        if not normalized_message:
            return False

        return self.finish_turn(
            normalized_message
        )
