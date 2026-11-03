"""
ZepMemoryAdapter
================

取代原本的 GlobalMemory，但保留相同介面：

- get
- set
- update_from_intent
- save
- to_summary

同時提供 GetZep 對話記憶能力：

- add_user_message
- add_assistant_message
- add_turn_to_zep
- get_context_block
- get_history
- clear_thread

多對話架構：

user_id
    使用者登入帳號，負責共用 Profile 與長期偏好。

conversation_id
    使用者帳號底下的某一個對話。

thread_id
    根據 user_id + conversation_id 產生，
    每個對話會對應不同 GetZep Thread。

這樣可以達成：

- 同一帳號共用膚質、過敏原、商品偏好
- 同一帳號擁有多個獨立對話
- 不同對話的原始訊息不會混在一起
"""

import hashlib
import json
import os

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from tools.getzep.zep_connection import (
    get_zep_client,
)

def _new_message(**kwargs):
    try:
        from zep_cloud.types import Message
    except ModuleNotFoundError as exc:
        raise RuntimeError("Zep memory requires the optional zep-cloud package") from exc
    return Message(**kwargs)


# =========================================================
# Default Profile
# =========================================================

DEFAULT_PROFILE = {
    "skin_type": None,
    "concerns": [],
    "budget_range": None,
    "disliked_ingredients": [],
    "preferred_categories": [],
    "product_experiences": [],
    "avoid_preferences": [],
    "preferred_features": [],
}


# =========================================================
# Adapter
# =========================================================

class ConversationStorageError(RuntimeError):
    """Safe public error for a failed conversation storage operation."""


class ZepMemoryAdapter:
    """
    GetZep 記憶轉接器。

    Profile：
        儲存在 GetZep User metadata，
        同一個 user_id 的所有對話共用。

    Conversation：
        每個 conversation_id 使用獨立 Thread。
    """

    def __init__(
        self,
        user_id: str,
        conversation_id: str = "default",
    ):
        self.user_id = self._normalize_user_id(
            user_id
        )

        self.conversation_id = (
            self._normalize_conversation_id(
                conversation_id
            )
        )

        self.thread_id = (
            self._build_thread_id(
                user_id=self.user_id,
                conversation_id=
                    self.conversation_id,
            )
        )

        self.client = get_zep_client()

        self._profile = deepcopy(
            DEFAULT_PROFILE
        )

        # Reading a page/history must not create a persistent conversation.
        self._pending_messages = []
        self._pending_title = ""
        self._pending_has_user_submission = False
        self._committed = False
        self._legacy_visibility = {}
        self._metadata_cache: Optional[dict] = None
        self._pg_turn_id = uuid4().hex
        metadata = self._get_user_metadata()
        self._load_profile(metadata)
        self._committed = any(
            item.get("conversation_id") == self.conversation_id
            for item in metadata.get("conversations", []) or []
        )


    # =====================================================
    # ID Utilities
    # =====================================================

    @staticmethod
    def _normalize_user_id(
        user_id: Optional[str],
    ) -> str:
        normalized = (
            str(
                user_id
                or "anonymous"
            )
            .strip()
        )

        return normalized or "anonymous"


    @staticmethod
    def _normalize_conversation_id(
        conversation_id: Optional[str],
    ) -> str:
        normalized = (
            str(
                conversation_id
                or "default"
            )
            .strip()
        )

        return normalized or "default"


    @staticmethod
    def _build_thread_id(
        user_id: str,
        conversation_id: str,
    ) -> str:
        """
        使用 hash 產生穩定且長度固定的 GetZep Thread ID。

        相同：
            user_id + conversation_id

        永遠會得到相同 thread_id。
        """

        source = (
            f"{user_id}:{conversation_id}"
        )

        digest = hashlib.sha256(
            source.encode(
                "utf-8"
            )
        ).hexdigest()[:48]

        return (
            f"blink_chat_{digest}"
        )


    @staticmethod
    def _utc_now() -> str:
        return datetime.now(
            timezone.utc
        ).isoformat()


    @staticmethod
    def _message_metadata_json_default(value):
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return model_dump(mode="json")
            except TypeError:
                return model_dump()

        legacy_dict = getattr(value, "dict", None)
        if callable(legacy_dict):
            return legacy_dict()

        return str(value)


    @classmethod
    def _prepare_message_metadata(
        cls,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Convert nested UI payloads into GetZep-supported scalar values."""

        prepared: dict = {}

        for key, value in (metadata or {}).items():
            if value is None:
                continue

            normalized_key = str(key)
            if isinstance(value, (str, int, float, bool)):
                prepared[normalized_key] = value
                continue

            prepared[normalized_key] = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=cls._message_metadata_json_default,
            )

        prepared["created_at"] = cls._utc_now()
        return prepared


    @staticmethod
    def _read_list_metadata(
        metadata: dict,
        key: str,
    ) -> list:
        value = metadata.get(key, [])

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return []

        return value if isinstance(value, list) else []


    # =====================================================
    # User / Thread Initialization
    # =====================================================

    def _ensure_user_and_thread(
        self,
    ) -> None:
        """
        確保 GetZep User 與本次 Conversation Thread 存在。

        User 或 Thread 已存在時，
        GetZep 可能拋出例外，此情況屬正常狀況。
        """

        try:
            self.client.user.add(
                user_id=self.user_id,
                email=(
                    f"{self.user_id}"
                    "@example.com"
                ),
            )
        except Exception:
            pass

        try:
            self.client.thread.create(
                thread_id=
                    self.thread_id,

                user_id=
                    self.user_id,
            )
        except Exception:
            pass


    # =====================================================
    # User Metadata
    # =====================================================

    def _get_user_metadata(
        self,
    ) -> dict:
        """Read user metadata once per adapter/request and reuse it."""
        if self._metadata_cache is not None:
            return deepcopy(self._metadata_cache)

        try:
            user = self.client.user.get(user_id=self.user_id)
            metadata = getattr(user, "metadata", None) or {}
            if isinstance(metadata, dict):
                self._metadata_cache = deepcopy(metadata)
                return deepcopy(metadata)
        except Exception as error:
            print(
                "[ZepMemoryAdapter] 讀取 User metadata 失敗："
                f"{type(error).__name__}"
            )

        self._metadata_cache = {}
        return {}


    def _save_user_metadata(
        self,
        metadata: dict,
    ) -> None:
        self.client.user.update(
            user_id=self.user_id,
            metadata=metadata,
        )
        self._metadata_cache = deepcopy(metadata)


    # =====================================================
    # Profile
    # =====================================================

    def _load_profile(
        self,
        metadata: Optional[dict] = None,
    ) -> None:
        """
        從 GetZep User metadata 載入 Profile。

        新使用者沒有 profile 時，
        使用 DEFAULT_PROFILE。
        """

        try:
            if metadata is None:
                metadata = self._get_user_metadata()

            saved_profile = (
                metadata.get(
                    "profile"
                )
            )

            if isinstance(
                saved_profile,
                dict,
            ):
                self._profile.update(
                    saved_profile
                )

        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "讀取 profile 失敗"
                "（新使用者可忽略）："
                f"{error}"
            )


    def get(
        self,
        key,
        default=None,
    ):
        return self._profile.get(
            key,
            default,
        )


    def set(
        self,
        key,
        value,
    ) -> None:
        """
        只更新記憶體中的 Profile。

        呼叫 save() 後才會寫回 GetZep。
        """

        self._profile[key] = value


    def update_from_intent(
        self,
        intent,
    ) -> None:
        """
        從 IntentParseResult 更新結構化 Profile。
        """

        if intent is None:
            return

        skin_type = getattr(
            intent,
            "skin_type",
            None,
        )

        skin_type_value = getattr(
            skin_type,
            "value",
            skin_type,
        )

        if (
            skin_type_value
            and skin_type_value
            != "unknown"
        ):
            self._profile[
                "skin_type"
            ] = skin_type_value


        concerns = getattr(
            intent,
            "concern",
            None,
        )

        if concerns:
            merged = (
                list(
                    self._profile.get(
                        "concerns",
                        [],
                    )
                    or []
                )
                + list(concerns)
            )

            self._profile[
                "concerns"
            ] = list(
                dict.fromkeys(
                    merged
                )
            )[-5:]


        budget_range = getattr(
            intent,
            "budget_range",
            None,
        )

        if budget_range:
            self._profile[
                "budget_range"
            ] = budget_range


        product_category = getattr(
            intent,
            "product_category",
            None,
        )

        category_value = getattr(
            product_category,
            "value",
            product_category,
        )

        if (
            category_value
            and category_value
            != "不明"
        ):
            categories = list(
                self._profile.get(
                    "preferred_categories",
                    [],
                )
                or []
            )

            if (
                category_value
                not in categories
            ):
                categories.append(
                    category_value
                )

            self._profile[
                "preferred_categories"
            ] = categories[-5:]


    def save(
        self,
    ) -> None:
        """
        將 Profile 寫入 GetZep User metadata。

        會保留 conversations 與其他 metadata，
        不會整份覆蓋掉。
        """

        try:
            metadata = (
                self._get_user_metadata()
            )

            metadata["profile"] = (
                deepcopy(
                    self._profile
                )
            )

            self._save_user_metadata(
                metadata
            )

        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "寫入 profile 失敗："
                f"{error}"
            )


    def to_summary(
        self,
    ) -> str:
        """
        將 Profile 整理為可交給 AI 的文字摘要。
        """

        parts: list[str] = []


        skin_type = self._profile.get(
            "skin_type"
        )

        if skin_type:
            parts.append(
                f"膚質：{skin_type}"
            )


        concerns = (
            self._profile.get(
                "concerns"
            )
            or []
        )

        if concerns:
            parts.append(
                "常見訴求："
                + "、".join(
                    concerns
                )
            )


        budget_range = (
            self._profile.get(
                "budget_range"
            )
        )

        if budget_range:
            parts.append(
                "預算偏好："
                f"{budget_range}"
            )


        disliked_ingredients = (
            self._profile.get(
                "disliked_ingredients"
            )
            or []
        )

        if disliked_ingredients:
            parts.append(
                "忌用成分："
                + "、".join(
                    disliked_ingredients
                )
            )


        preferred_categories = (
            self._profile.get(
                "preferred_categories"
            )
            or []
        )

        if preferred_categories:
            parts.append(
                "常買品類："
                + "、".join(
                    preferred_categories
                )
            )


        avoid_preferences = (
            self._profile.get(
                "avoid_preferences"
            )
            or []
        )

        if avoid_preferences:
            parts.append(
                "應避開偏好："
                + "、".join(
                    avoid_preferences
                )
            )


        preferred_features = (
            self._profile.get(
                "preferred_features"
            )
            or []
        )

        if preferred_features:
            parts.append(
                "偏好特徵："
                + "、".join(
                    preferred_features
                )
            )


        experiences = (
            self._profile.get(
                "product_experiences"
            )
            or []
        )

        if experiences:
            recent: list[str] = []

            for item in experiences[-5:]:
                product_name = (
                    item.get(
                        "product_name"
                    )
                    or "未命名商品"
                )

                brand_name = (
                    item.get(
                        "brand_name"
                    )
                    or ""
                )

                if (
                    brand_name
                    and brand_name
                    not in product_name
                ):
                    display_name = (
                        f"{brand_name} "
                        f"{product_name}"
                    ).strip()
                else:
                    display_name = (
                        product_name
                    )

                attributes = "、".join(
                    item.get(
                        "attributes"
                    )
                    or []
                )

                sentiment = (
                    item.get(
                        "sentiment"
                    )
                    or "unknown"
                )

                detail = (
                    attributes
                    or sentiment
                )

                recent.append(
                    f"{display_name}"
                    f"（{detail}）"
                )

            parts.append(
                "商品使用經驗："
                + "；".join(
                    recent
                )
            )


        return "；".join(
            parts
        )


    # =====================================================
    # Product Experience
    # =====================================================

    def record_product_experience(
        self,
        product_name: str,
        brand_name: str = "",
        sentiment: str = "unknown",
        attributes: Optional[list] = None,
        avoid_preferences:
            Optional[list] = None,
        preferred_features:
            Optional[list] = None,
    ) -> None:
        """
        保存使用者明確表達的商品使用經驗。
        """

        product_name = (
            product_name
            or ""
        ).strip()

        brand_name = (
            brand_name
            or ""
        ).strip()

        attributes = [
            str(item).strip()
            for item in (
                attributes
                or []
            )
            if str(item).strip()
        ]

        avoid_preferences = [
            str(item).strip()
            for item in (
                avoid_preferences
                or []
            )
            if str(item).strip()
        ]

        preferred_features = [
            str(item).strip()
            for item in (
                preferred_features
                or []
            )
            if str(item).strip()
        ]

        if not product_name:
            return


        allowed_sentiments = {
            "positive",
            "negative",
            "neutral",
            "unknown",
        }

        normalized_sentiment = (
            sentiment
            if sentiment
            in allowed_sentiments
            else "unknown"
        )


        experiences = list(
            self._profile.get(
                "product_experiences"
            )
            or []
        )

        entry = {
            "product_name":
                product_name,

            "brand_name":
                brand_name,

            "sentiment":
                normalized_sentiment,

            "attributes":
                list(
                    dict.fromkeys(
                        attributes
                    )
                )[:10],
        }

        if entry not in experiences:
            experiences.append(
                entry
            )

        self._profile[
            "product_experiences"
        ] = experiences[-20:]


        old_avoid = list(
            self._profile.get(
                "avoid_preferences"
            )
            or []
        )

        self._profile[
            "avoid_preferences"
        ] = list(
            dict.fromkeys(
                old_avoid
                + avoid_preferences
            )
        )[-20:]


        old_preferred = list(
            self._profile.get(
                "preferred_features"
            )
            or []
        )

        self._profile[
            "preferred_features"
        ] = list(
            dict.fromkeys(
                old_preferred
                + preferred_features
            )
        )[-20:]


        self.save()


    # =====================================================
    # Conversation Registry
    # =====================================================

    def _ensure_conversation_record(
        self,
    ) -> None:
        """
        確保 User metadata 的 conversations 清單中，
        存在目前 conversation_id。
        """

        metadata = (
            self._get_user_metadata()
        )

        conversations = list(
            metadata.get(
                "conversations"
            )
            or []
        )

        exists = any(
            item.get(
                "conversation_id"
            )
            == self.conversation_id
            for item in conversations
        )

        if exists:
            return


        now = self._utc_now()

        conversations.insert(
            0,
            {
                "conversation_id":
                    self.conversation_id,

                "title":
                    "新對話",

                "created_at":
                    now,

                "updated_at":
                    now,
            },
        )

        metadata[
            "conversations"
        ] = conversations[:100]

        if (
            "profile"
            not in metadata
        ):
            metadata["profile"] = (
                deepcopy(
                    self._profile
                )
            )

        try:
            self._save_user_metadata(
                metadata
            )
        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "建立對話索引失敗："
                f"{error}"
            )


    def list_conversations(
        self,
    ) -> list:
        """
        列出目前使用者的對話清單，
        依最後更新時間由新到舊排列。
        """
        conversations, _ = self.list_conversations_page(
            limit=100,
            offset=0,
        )
        return conversations

    def list_conversations_page(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list, int]:
        """Return one page and the visible total from the Zep user index.

        Zep stores this bounded index (at most 100 rows) in one user-metadata
        document. Pagination is therefore applied here at the storage adapter,
        so service and HTTP layers never materialize the full visible result.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")

        metadata = self._get_user_metadata()
        conversations = list(metadata.get("conversations") or [])
        conversations.sort(
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

        # New records persist has_user_message=True. Legacy records predate that
        # field; keep them visible instead of issuing one Zep history read per row.
        # Explicit False remains the only hidden state for abandoned drafts.
        page = []
        total = 0
        page_end = offset + limit
        for item in conversations:
            if item.get("has_user_message") is False:
                continue
            if offset <= total < page_end:
                page.append(item)
            total += 1
        return page, total

    def count_conversations(self) -> int:
        """Count visible conversations without returning the entire index."""
        _, total = self.list_conversations_page(limit=1, offset=0)
        return total

    @staticmethod
    def _is_user_submission(content: str) -> bool:
        normalized = str(content or "").strip()
        return bool(normalized) and normalized not in {
            "cycle_skin_tracking",
            "我要記錄膚況",
            "我要紀錄膚況",
            "我要記錄今天的膚況",
            "我要紀錄今天的膚況",
            "開始記錄膚況",
            "開始紀錄膚況",
        }

    def rename_conversation(self, conversation_id: str, title: str) -> dict:
        title = " ".join(title.split())
        if not title or len(title) > 80:
            raise ValueError("標題需為 1～80 個字")
        metadata = self._get_user_metadata()
        for item in metadata.get("conversations", []) or []:
            if item.get("conversation_id") == conversation_id:
                item["title"] = title
                item["title_source"] = "manual"
                self._save_user_metadata(metadata)
                return dict(item)
        raise KeyError(conversation_id)


    def delete_conversation(
        self,
        conversation_id:
            Optional[str] = None,
    ) -> bool:
        """
        永久刪除指定 conversation_id 的對話。

        會刪除：

        - 對應的 GetZep Thread
        - User metadata 中的 conversations 清單紀錄

        不會刪除：

        - 使用者 Profile
        - 其他 conversation_id 的 Thread

        回傳：

        - True：找到對話並完成刪除
        - False：conversations 清單中沒有該對話
        """

        target_conversation_id = (
            self._normalize_conversation_id(
                conversation_id
                or self.conversation_id
            )
        )

        metadata = (
            self._get_user_metadata()
        )

        conversations = list(
            metadata.get(
                "conversations"
            )
            or []
        )

        conversation_exists = any(
            item.get(
                "conversation_id"
            )
            == target_conversation_id
            for item in conversations
        )

        if not conversation_exists:
            return False

        target_thread_id = (
            self._build_thread_id(
                user_id=self.user_id,
                conversation_id=
                    target_conversation_id,
            )
        )

        try:
            self.client.thread.delete(
                thread_id=
                    target_thread_id
            )

        except Exception as error:
            # Fail closed: do not hide a thread that may still contain data.
            # Only the provider exception type is logged; its message may
            # contain internal identifiers and must not reach API clients.
            print(
                "[ZepMemoryAdapter] "
                "conversation thread delete failed: "
                f"{type(error).__name__}"
            )
            raise ConversationStorageError(
                "對話刪除暫時無法完成，請稍後再試。"
            ) from None

        metadata[
            "conversations"
        ] = [
            item
            for item in conversations
            if item.get(
                "conversation_id"
            )
            != target_conversation_id
        ]

        # 使用原本取得的完整 metadata，
        # 因此 Profile 與其他欄位都會保留。
        self._save_user_metadata(
            metadata
        )

        return True


    def touch_conversation(
        self,
        first_message:
            Optional[str] = None,
    ) -> None:
        """
        更新目前對話的 updated_at。

        如果目前標題仍是「新對話」，
        使用第一則訊息前 24 個字作為標題。
        """

        if not self._committed:
            if first_message:
                self._pending_title = first_message
            return

        # Conversation agents can live for a long time. Always merge against the
        # latest index so one tab cannot overwrite conversations created by another.
        self._metadata_cache = None
        metadata = self._get_user_metadata()

        conversations = list(
            metadata.get(
                "conversations"
            )
            or []
        )

        now = self._utc_now()

        normalized_message = (
            " ".join(
                (
                    first_message
                    or ""
                ).split()
            )
        )

        found = False


        for item in conversations:
            if (
                item.get(
                    "conversation_id"
                )
                != self.conversation_id
            ):
                continue

            found = True
            item["has_user_message"] = True

            item["updated_at"] = now

            current_title = (
                item.get(
                    "title"
                )
                or ""
            ).strip()

            if (
                normalized_message
                and item.get("title_source") != "manual"
                and (
                    not current_title
                    or current_title
                    == "新對話"
                )
            ):
                item["title"] = (
                    normalized_message[:24]
                )

            break


        if not found:
            conversations.append(
                {
                    "conversation_id":
                        self.conversation_id,

                    "has_user_message": True,

                    "title":
                        (
                            normalized_message[:24]
                            or "新對話"
                        ),

                    "created_at":
                        now,

                    "updated_at":
                        now,
                }
            )


        conversations.sort(
            key=lambda item:
                item.get(
                    "updated_at",
                    "",
                ),
            reverse=True,
        )

        metadata[
            "conversations"
        ] = conversations[:100]

        metadata["profile"] = (
            deepcopy(
                self._profile
            )
        )

        try:
            self._save_user_metadata(
                metadata
            )
        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "更新對話索引失敗："
                f"{error}"
            )


    def _mirror_message_to_postgres(
        self,
        *,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Best-effort durable mirror; never logs message content."""
        if os.getenv("AUTH_STORAGE_BACKEND", "postgres").strip().lower() != "postgres":
            return False
        try:
            from database.postgres.repositories.conversation import (
                postgres_conversation_repository,
            )
            from database.postgres.repositories.conversation_message import (
                postgres_conversation_message_repository,
            )

            postgres_conversation_repository.ensure(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
            )
            saved = postgres_conversation_message_repository.append(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                role=role,
                content=content,
                request_id=self._pg_turn_id,
                metadata=metadata or {},
            )
            if saved:
                print(
                    "[ZepMemoryAdapter] PostgreSQL message mirror saved, "
                    f"role={role}, account_hash={hashlib.sha256(self.user_id.encode('utf-8')).hexdigest()[:16]}",
                )
            return saved
        except Exception as error:
            print(
                "[ZepMemoryAdapter] PostgreSQL message mirror failed: "
                f"{type(error).__name__}"
            )
            return False


    # =====================================================
    # GetZep Messages
    # =====================================================

    def add_user_message(
        self,
        content: str,
    ) -> None:
        """Queue the first user message; persist later turns immediately."""
        normalized = (content or "").strip()
        if not normalized:
            raise ValueError("user message 不可為空")

        if self._is_user_submission(normalized):
            self._mirror_message_to_postgres(
                role="user",
                content=normalized,
            )

        message = _new_message(
            role="user",
            name=self.user_id,
            content=normalized,
            metadata={"created_at": self._utc_now()},
        )
        if not self._committed:
            if not self._is_user_submission(normalized):
                self._pending_messages = [message]
                self._pending_has_user_submission = False
                return
            self._pending_messages.append(message)
            self._pending_has_user_submission = True
            if not self._pending_title:
                self._pending_title = normalized
            return

        self.client.thread.add_messages(
            thread_id=self.thread_id,
            messages=[message],
        )
        self.touch_conversation(first_message=normalized)

    def add_assistant_message(
        self,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Persist a new conversation atomically after its first complete reply."""
        normalized = (content or "").strip()
        if not normalized:
            raise ValueError("assistant message 不可為空")

        message = _new_message(
            role="assistant",
            name="Assistant",
            content=normalized,
            metadata=self._prepare_message_metadata(metadata),
        )
        if not self._committed:
            if not self._pending_has_user_submission:
                self._pending_messages.append(message)
                return
            self._ensure_user_and_thread()
            self.client.thread.add_messages(
                thread_id=self.thread_id,
                messages=[*self._pending_messages, message],
            )
            self._pending_messages = []
            self._pending_has_user_submission = False
            self._committed = True
            self.touch_conversation(first_message=self._pending_title)
            self._pending_title = ""
            self._mirror_message_to_postgres(
                role="assistant",
                content=normalized,
                metadata=metadata,
            )
            return

        self.client.thread.add_messages(
            thread_id=self.thread_id,
            messages=[message],
        )
        self.touch_conversation()
        self._mirror_message_to_postgres(
            role="assistant",
            content=normalized,
            metadata=metadata,
        )


    def add_turn_to_zep(
        self,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """
        舊介面相容層。

        新流程建議使用：

        ConversationMemoryService.begin_turn()
        ConversationMemoryService.finish_turn()
        """

        try:
            user_text = (
                user_content
                or ""
            ).strip()

            assistant_text = (
                assistant_content
                or ""
            ).strip()

            if not user_text:
                return

            self.add_user_message(user_text)
            if assistant_text:
                self.add_assistant_message(assistant_text)

        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "寫入對話失敗："
                f"{error}"
            )


    def get_context_block(
        self,
    ) -> str:
        """
        取得目前 conversation_id 對應 Thread 的近期原始訊息。

        不使用 user-wide AI Context，避免其他 conversation 的內容被帶入。
        """

        try:
            history = self.get_history(
                lastn=40,
            )

            # begin_turn() 會先保存本輪 user message，再取得 context。
            # 本輪問題會另外傳給模型，因此從歷史 context 移除它。
            if (
                history
                and history[-1].get("role") == "user"
            ):
                history = history[:-1]

            recent_user_messages = [
                message
                for message in history
                if message.get("role") == "user"
            ][-8:]
            lines: list[str] = []
            for message in recent_user_messages:
                content = str(
                    message.get("content")
                    or ""
                ).strip()
                if not content:
                    continue

                lines.append(
                    "使用者先前訊息："
                    f"{content[:500]}"
                )

            return "\n".join(lines)

        except Exception as error:
            print(
                "[ZepMemoryAdapter] "
                "取得 context 失敗："
                f"{error}"
            )

            return ""


    def get_history(
        self,
        lastn: int = 100,
    ) -> list:
        """
        取得目前 Thread 最近的原始訊息，
        供前端還原對話。
        """

        safe_lastn = max(
            1,
            min(
                int(lastn),
                200,
            ),
        )

        try:
            response = self.client.thread.get(
                thread_id=
                    self.thread_id,

                lastn=
                    safe_lastn,
            )
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return []
            raise

        messages = (
            getattr(
                response,
                "messages",
                None,
            )
            or []
        )

        history: list[dict] = []


        for index, message in enumerate(
            messages
        ):
            role = getattr(
                message,
                "role",
                None,
            )

            role = getattr(
                role,
                "value",
                role,
            )

            content = getattr(
                message,
                "content",
                None,
            )

            if (
                role not in (
                    "user",
                    "assistant",
                )
                or not content
            ):
                continue

            message_id = (
                getattr(
                    message,
                    "uuid",
                    None,
                )
                or getattr(
                    message,
                    "id",
                    None,
                )
                or (
                    f"zep-"
                    f"{self.conversation_id}-"
                    f"{index}"
                )
            )

            metadata = getattr(message, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}

            quick_replies = self._read_list_metadata(
                metadata,
                "quick_replies",
            )
            cards = self._read_list_metadata(
                metadata,
                "cards",
            )

            history.append(
                {
                    "id": str(message_id),
                    "role": role,
                    "content": str(content),
                    "metadata": metadata,
                    "created_at": metadata.get("created_at") or (
                        str(getattr(message, "created_at", "") or "") or None
                    ),
                    "quick_replies": quick_replies,
                    "cards": cards,
                }
            )


        return history


    def clear_thread(
        self,
    ) -> None:
        """
        清除目前 conversation_id 的 Thread。

        只會清除目前對話，
        不會清除：

        - 使用者 Profile
        - 其他 conversation_id 的 Thread
        - conversations 清單
        """

        try:
            self.client.thread.delete(
                thread_id=
                    self.thread_id
            )
        except Exception:
            pass

        self.client.thread.create(
            thread_id=
                self.thread_id,

            user_id=
                self.user_id,
        )

        self.touch_conversation()
