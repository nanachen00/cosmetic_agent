"""Bridge between the canonical member profile and conversational agents.

The primary profile remains in PostgreSQL (or the legacy Neo4j repository).
Zep only receives the small subset needed by the agents.  This module also
handles deterministic self-profile questions and conservative, first-person
profile updates from chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import os
import re
from typing import Any, Protocol


SKIN_TYPE_ALIASES = {
    "oily": "oily",
    "油性肌": "oily",
    "油性肌膚": "oily",
    "油肌": "oily",
    "dry": "dry",
    "乾性肌": "dry",
    "乾性肌膚": "dry",
    "乾肌": "dry",
    "combination": "combination",
    "混合肌": "combination",
    "混合性肌": "combination",
    "混合性肌膚": "combination",
    "sensitive": "sensitive",
    "敏感肌": "sensitive",
    "敏感性肌": "sensitive",
    "敏感性肌膚": "sensitive",
    "normal": "normal",
    "中性肌": "normal",
    "中性肌膚": "normal",
}

SKIN_TYPE_LABELS = {
    "oily": "油性肌",
    "dry": "乾性肌",
    "combination": "混合肌",
    "sensitive": "敏感肌",
    "normal": "中性肌",
}

CUSTOMER_TYPE_LABELS = {
    "hesitant_decision_maker": "猶豫決策型",
    "ingredient_researcher": "成分研究型",
    "price_sensitive": "價格敏感型",
    "review_dependent": "評價依賴型",
    "brand_oriented": "品牌導向型",
    "explorer": "探索嘗鮮型",
    "function_oriented": "功效導向型",
    "impulse_buyer": "衝動購買型",
}

_SKIN_PATTERN = (
    r"油性肌膚|油性肌|油肌|乾性肌膚|乾性肌|乾肌|"
    r"混合性肌膚|混合性肌|混合肌|"
    r"敏感性肌膚|敏感性肌|敏感肌|中性肌膚|中性肌"
)

_UNCERTAIN_MARKERS = (
    "如果",
    "假如",
    "可能",
    "也許",
    "好像",
    "不確定",
    "不知道",
    "懷疑",
    "是否",
    "會不會",
    "嗎",
)

_GENERIC_ALLERGEN_WORDS = {
    "",
    "什麼",
    "哪些",
    "哪個",
    "哪一些",
    "成分",
    "東西",
    "任何東西",
    "任何成分",
}


class ProfileRepositoryProtocol(Protocol):
    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        ...

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass
class ProfileSyncResult:
    profile: dict[str, Any] = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)
    acknowledgement: str = ""
    update_only: bool = False
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields) and not self.error


def normalize_skin_type(value: Any) -> str:
    text = str(value or "").strip()
    return SKIN_TYPE_ALIASES.get(text.lower(), SKIN_TYPE_ALIASES.get(text, ""))


def skin_type_label(value: Any) -> str:
    normalized = normalize_skin_type(value)
    if normalized:
        return SKIN_TYPE_LABELS[normalized]
    return str(value or "").strip()


def _unique_text_list(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = []
    normalized = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _default_profile(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "name": "",
        "gender": "",
        "birthday": "",
        "email": "",
        "lineBound": False,
        "skinType": "",
        "allergens": [],
        "otherAllergen": "",
        "customerType": "",
        "wellnessStatus": "",
        "periodStartDate": "",
        "periodEndDate": "",
    }


class ProfileContextBridge:
    """Load, describe and conservatively update the canonical user profile."""

    def __init__(
        self,
        user_id: str,
        repository: ProfileRepositoryProtocol | None = None,
    ) -> None:
        self.user_id = str(user_id or "anonymous").strip() or "anonymous"
        self._repository_override = repository
        self._quiz_lookup_done = False
        self._quiz_customer_type = ""

    def _repository(self) -> ProfileRepositoryProtocol:
        if self._repository_override is not None:
            return self._repository_override

        backend = os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower()
        if backend == "postgres":
            from database.postgres.repositories.profile import (
                postgres_profile_repository,
            )

            return postgres_profile_repository

        from database.profile_repository import profile_repository

        return profile_repository

    def normalize_profile(self, profile: dict[str, Any] | None) -> dict[str, Any]:
        normalized = _default_profile(self.user_id)
        if profile:
            normalized.update(profile)

        normalized["user_id"] = str(
            normalized.get("user_id") or self.user_id
        )
        normalized["allergens"] = _unique_text_list(
            normalized.get("allergens", [])
        )
        normalized["otherAllergen"] = str(
            normalized.get("otherAllergen", "") or ""
        ).strip()
        return normalized

    def _latest_quiz_customer_type(self) -> str:
        """Read one legacy quiz result when it has not been mirrored yet."""
        if self._quiz_lookup_done:
            return self._quiz_customer_type

        self._quiz_lookup_done = True
        if self._repository_override is not None or self.user_id == "anonymous":
            return ""

        try:
            from tools.user.quiz_analysis_tool import get_latest_quiz_result

            latest = get_latest_quiz_result(self.user_id)
            result = latest.get("result") if isinstance(latest, dict) else None
            if isinstance(result, dict):
                self._quiz_customer_type = str(
                    result.get("primary_type", "") or ""
                ).strip()
        except Exception:
            # Basic-profile queries must continue even if the quiz store is down.
            self._quiz_customer_type = ""

        return self._quiz_customer_type

    def load_profile(self) -> dict[str, Any]:
        if self.user_id == "anonymous":
            return _default_profile(self.user_id)

        repository = self._repository()
        profile = self.normalize_profile(
            repository.get_profile(user_id=self.user_id)
        )

        # Older quiz attempts were stored separately and therefore may not have
        # customerType in the profile yet. Read the latest result once and mirror
        # it forward so both old and new members get the same deterministic reply.
        if not str(profile.get("customerType", "") or "").strip():
            customer_type = self._latest_quiz_customer_type()
            if customer_type:
                profile["customerType"] = customer_type
                update_method = getattr(repository, "update_customer_type", None)
                if callable(update_method):
                    try:
                        saved = update_method(
                            user_id=self.user_id,
                            customer_type=customer_type,
                        )
                        profile = self.normalize_profile(saved)
                    except Exception:
                        # The current turn may still safely use the verified quiz
                        # result; a future profile save can repair the mirror.
                        pass

        return profile

    def apply_to_memory(self, profile: dict[str, Any], memory: Any) -> None:
        """Copy only recommendation-safe fields into Zep agent memory."""
        memory.set("skin_type", normalize_skin_type(profile.get("skinType")) or None)
        memory.set(
            "disliked_ingredients",
            _unique_text_list(profile.get("allergens", [])),
        )

    def refresh(self, memory: Any | None = None) -> dict[str, Any]:
        profile = self.load_profile()
        if memory is not None:
            self.apply_to_memory(profile, memory)
        return profile

    @staticmethod
    def _age_from_birthday(value: Any) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            birthday = date.fromisoformat(text)
        except ValueError:
            return None
        today = date.today()
        return today.year - birthday.year - (
            (today.month, today.day) < (birthday.month, birthday.day)
        )

    def build_prompt_context(self, profile: dict[str, Any]) -> str:
        """Build verified context without sending login identifiers to an LLM."""
        items: list[str] = []
        name = str(profile.get("name", "") or "").strip()
        gender = str(profile.get("gender", "") or "").strip()
        skin_type = skin_type_label(profile.get("skinType"))
        allergens = _unique_text_list(profile.get("allergens", []))
        customer_type = str(profile.get("customerType", "") or "").strip()
        wellness = str(profile.get("wellnessStatus", "") or "").strip()
        period_start = str(profile.get("periodStartDate", "") or "").strip()
        period_end = str(profile.get("periodEndDate", "") or "").strip()
        age = self._age_from_birthday(profile.get("birthday"))

        if name:
            items.append(f"姓名：{name}")
        if gender and gender != "不透露":
            items.append(f"性別：{gender}")
        if age is not None:
            items.append(f"年齡：{age} 歲")
        if skin_type:
            items.append(f"膚質：{skin_type}")
        if allergens:
            items.append("已知過敏／禁忌成分：" + "、".join(allergens))
        if wellness:
            items.append(f"目前身體狀態：{wellness}")
        if period_start:
            items.append(f"最近一次月經開始日：{period_start}")
        if period_end:
            items.append(f"最近一次月經結束日：{period_end}")
        if customer_type:
            items.append(
                "消費人格："
                + CUSTOMER_TYPE_LABELS.get(customer_type, customer_type)
            )

        if not items:
            return ""

        return (
            "【會員基本資料（已驗證）】\n"
            + "\n".join(f"- {item}" for item in items)
            + "\n回答與推薦時只在相關情境引用；"
            "若本輪明確陳述與資料不同，以本輪陳述為準。"
        )

    @staticmethod
    def _message_text(message: str) -> str:
        text = str(message or "").strip()
        if text.startswith("__knowledge_consult__:"):
            text = text.split(":", 1)[1].strip()
        return text

    @staticmethod
    def _clauses(message: str) -> list[str]:
        return [
            item.strip()
            for item in re.split(r"[，,。！!？?；;\n]+", message)
            if item.strip()
        ]

    @staticmethod
    def _is_certain_clause(clause: str) -> bool:
        return not any(marker in clause for marker in _UNCERTAIN_MARKERS)

    @staticmethod
    def _split_allergens(value: str) -> list[str]:
        text = re.sub(r"^(?:成分|過敏原)(?:是|有|包括)?", "", value.strip())
        text = re.sub(r"(?:這些)?成分$", "", text).strip()
        pieces = re.split(r"[、,，/／]|以及|還有|跟|與|和|及", text)
        cleaned: list[str] = []
        for piece in pieces:
            item = piece.strip(" ：:的")
            if (
                item not in _GENERIC_ALLERGEN_WORDS
                and 0 < len(item) <= 40
                and item not in cleaned
            ):
                cleaned.append(item)
        return cleaned

    def _extract_updates(
        self,
        message: str,
        current_profile: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        skin_type: str | None = None
        allergens = _unique_text_list(current_profile.get("allergens", []))
        allergens_changed = False

        for clause in self._clauses(message):
            if not self._is_certain_clause(clause):
                continue

            skin_match = re.search(
                rf"(?:我的(?:膚質|皮膚類型)(?:目前)?(?:是|屬於)|"
                rf"我(?:目前)?是)\s*({_SKIN_PATTERN})",
                clause,
            )
            if skin_match:
                skin_type = skin_type_label(skin_match.group(1))

            replacement_match = re.search(
                rf"我不是\s*(?:{_SKIN_PATTERN})\s*(?:而是|是)\s*({_SKIN_PATTERN})",
                clause,
            )
            if replacement_match:
                skin_type = skin_type_label(replacement_match.group(1))

            if re.search(r"我沒有(?:任何)?(?:過敏|過敏原)", clause):
                if allergens:
                    allergens = []
                    allergens_changed = True
                continue

            removal_match = re.search(
                r"我(?:已經)?不再?對(.+?)(?:過敏|敏感|不耐受)",
                clause,
            ) or re.search(
                r"我對(.+?)(?:不過敏|不敏感|沒有過敏)",
                clause,
            ) or re.search(
                r"我沒有對(.+?)(?:過敏|敏感|不耐受)",
                clause,
            )
            if removal_match:
                for item in self._split_allergens(removal_match.group(1)):
                    if item in allergens:
                        allergens.remove(item)
                        allergens_changed = True
                continue

            addition_match = re.search(
                r"我對(.+?)(?:會過敏|過敏|敏感|不耐受)",
                clause,
            ) or re.search(
                r"我的過敏原(?:是|有|包括)(.+)$",
                clause,
            )
            if addition_match:
                for item in self._split_allergens(addition_match.group(1)):
                    if item not in allergens:
                        allergens.append(item)
                        allergens_changed = True

        updates: dict[str, Any] = {}
        changed_fields: list[str] = []

        if skin_type and skin_type != skin_type_label(current_profile.get("skinType")):
            updates["skinType"] = skin_type
            changed_fields.append("skinType")

        if allergens_changed:
            updates["allergens"] = allergens
            changed_fields.append("allergens")

        return updates, changed_fields

    @staticmethod
    def _is_update_only(message: str) -> bool:
        request_markers = (
            "推薦",
            "適合",
            "可以用",
            "能用",
            "怎麼",
            "如何",
            "為什麼",
            "什麼產品",
            "哪個產品",
            "幫我找",
            "幫我查",
            "請問",
        )
        return not any(marker in message for marker in request_markers)

    def _save_skin_updates(
        self,
        current_profile: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        repository = self._repository()
        update_method = getattr(repository, "update_skin_profile", None)
        if callable(update_method):
            return update_method(
                user_id=self.user_id,
                skin_type=updates.get("skinType") if "skinType" in updates else None,
                allergens=updates.get("allergens") if "allergens" in updates else None,
            )

        merged = dict(current_profile)
        merged.update(updates)
        return repository.save_profile(user_id=self.user_id, profile=merged)

    def sync_explicit_facts(
        self,
        message: str,
        current_profile: dict[str, Any] | None = None,
        memory: Any | None = None,
    ) -> ProfileSyncResult:
        text = self._message_text(message)
        profile = self.normalize_profile(current_profile) if current_profile else self.load_profile()
        updates, changed_fields = self._extract_updates(text, profile)
        if not changed_fields or self.user_id == "anonymous":
            return ProfileSyncResult(profile=profile)

        update_only = self._is_update_only(text)
        try:
            saved = self.normalize_profile(self._save_skin_updates(profile, updates))
            if memory is not None:
                self.apply_to_memory(saved, memory)
        except Exception as error:
            return ProfileSyncResult(
                profile=profile,
                changed_fields=changed_fields,
                acknowledgement="我有理解您提供的資料，但目前無法同步到基本資料，請稍後再試。",
                update_only=update_only,
                error=str(error),
            )

        details: list[str] = []
        if "skinType" in changed_fields:
            details.append(f"膚質：{skin_type_label(saved.get('skinType'))}")
        if "allergens" in changed_fields:
            saved_allergens = _unique_text_list(saved.get("allergens", []))
            details.append(
                "過敏／禁忌成分："
                + ("、".join(saved_allergens) if saved_allergens else "目前無")
            )

        return ProfileSyncResult(
            profile=saved,
            changed_fields=changed_fields,
            acknowledgement="已同步更新您的基本資料：" + "；".join(details) + "。",
            update_only=update_only,
        )

    def answer_profile_question(
        self,
        message: str,
        profile: dict[str, Any],
    ) -> str | None:
        text = re.sub(r"[\s，,。！!？?；;]+", "", self._message_text(message))

        if re.search(r"我(?:是)?什麼膚質|我的(?:膚質|皮膚類型)(?:是)?什麼|我的膚質是哪種", text):
            label = skin_type_label(profile.get("skinType"))
            return (
                f"您在基本資料中登記的膚質是「{label}」。"
                if label
                else "您的基本資料目前還沒有設定膚質。"
            )

        if re.search(
            r"我(?:對)?(?:什麼|甚麼|啥|哪些|哪一些|哪種|何種)"
            r"(?:東西|成分|過敏原)?(?:會)?"
            r"(?:過敏|敏感|不耐受)"
            r"|我有哪些(?:過敏|過敏原|禁忌成分)"
            r"|我的過敏原(?:是什麼|是甚麼|有哪些|呢)"
            r"|我的禁忌成分(?:是什麼|是甚麼|有哪些|呢)"
            r"|我(?:不能用|需要避開|要避開)"
            r"(?:什麼|甚麼|啥|哪些|哪一些|哪種)"
            r"(?:東西|成分)?",
            text,
        ):
            allergens = _unique_text_list(
                profile.get("allergens", [])
            )
        
            if allergens:
                return (
                    "您在基本資料中登記的過敏／禁忌成分是：「"
                    + "、".join(allergens)
                    + "」。推薦商品時我會避開這些成分。"
                )
        
            return "您的基本資料目前沒有登記過敏／禁忌成分。"

        if re.search(
            r"我(?:是)?什麼消費(?:者)?人格"
            r"|我的消費(?:者)?人格"
            r"(?:是什麼|是甚麼|是哪種|呢)",
            text,
        ):
            customer_type = str(
                profile.get("customerType", "") or ""
            ).strip()
        
            label = CUSTOMER_TYPE_LABELS.get(
                customer_type,
                customer_type,
            )
        
            if label:
                return f"您目前的消費人格是「{label}」。"
        
            return (
                "您目前還沒有完成消費人格測驗，"
                "因此沒有可確認的結果。"
            )

        if re.search(r"我的名字(?:是什麼|叫什麼)|我叫什麼名字", text):
            name = str(profile.get("name", "") or "").strip()
            return f"您的基本資料姓名是「{name}」。" if name else "您的基本資料目前沒有填寫姓名。"

        if re.search(r"我的生日(?:是什麼|是哪天|呢)", text):
            birthday = str(profile.get("birthday", "") or "").strip()
            return f"您的基本資料生日是 {birthday}。" if birthday else "您的基本資料目前沒有填寫生日。"

        if re.search(r"我的(?:電子郵件|email|Email)(?:是什麼|呢)", text):
            email = str(profile.get("email", "") or "").strip()
            return f"您目前綁定的 Email 是 {email}。" if email else "您的基本資料目前沒有可顯示的 Email。"

        if re.search(r"我的性別(?:是什麼|呢)", text):
            gender = str(profile.get("gender", "") or "").strip()
            return f"您的基本資料性別是「{gender}」。" if gender else "您的基本資料目前沒有設定性別。"

        if re.search(r"我現在是什麼身體狀態|我的身體狀態(?:是什麼|呢)", text):
            wellness = str(profile.get("wellnessStatus", "") or "").strip()
            return (
                f"您在基本資料中登記的目前身體狀態是「{wellness}」。"
                if wellness
                else "您的基本資料目前沒有設定身體狀態。"
            )

        if re.search(r"我的(?:生理期|月經)(?:日期|紀錄|是什麼|呢)", text):
            period_start = str(profile.get("periodStartDate", "") or "").strip()
            period_end = str(profile.get("periodEndDate", "") or "").strip()
            if period_start and period_end:
                return f"您最近一次月經紀錄是 {period_start} 至 {period_end}。"
            if period_start:
                return f"您最近一次月經開始日是 {period_start}，目前未登記結束日。"
            return "您的基本資料目前沒有月經週期日期紀錄。"

        if re.search(r"我的基本資料|我的個人資料", text):
            lines: list[str] = []
            for label, key in (
                ("姓名", "name"),
                ("性別", "gender"),
                ("生日", "birthday"),
                ("Email", "email"),
            ):
                value = str(profile.get(key, "") or "").strip()
                if value:
                    lines.append(f"{label}：{value}")
            skin = skin_type_label(profile.get("skinType"))
            if skin:
                lines.append(f"膚質：{skin}")
            allergens = _unique_text_list(profile.get("allergens", []))
            if allergens:
                lines.append("過敏／禁忌成分：" + "、".join(allergens))
            customer_type = str(profile.get("customerType", "") or "").strip()
            if customer_type:
                lines.append(
                    "消費人格："
                    + CUSTOMER_TYPE_LABELS.get(customer_type, customer_type)
                )
            wellness = str(profile.get("wellnessStatus", "") or "").strip()
            if wellness:
                lines.append(f"目前身體狀態：{wellness}")
            return (
                "您目前的基本資料如下：\n" + "\n".join(f"- {line}" for line in lines)
                if lines
                else "您的基本資料目前還沒有可顯示的內容。"
            )

        return None
