"""PostgreSQL-backed user analysis context with no MongoDB dependency."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from enum import Enum
from typing import Any

from database.postgres.connection import postgres_connection


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "dict"):
        return _json_safe(value.dict())
    return str(value)


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


class UserAnalysisContextBridge:
    """Collect small personalization signals without blocking the main answer."""

    ANALYZE_EVERY = 3

    def __init__(self, user_id: str, conversation_id: str = "default") -> None:
        self.user_id = str(user_id or "anonymous").strip() or "anonymous"
        self.conversation_id = str(conversation_id or "default").strip() or "default"

    @property
    def enabled(self) -> bool:
        return self.user_id != "anonymous"

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with postgres_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO user_behavior_events (
                            user_id, conversation_id, event_type, payload
                        ) VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (
                            self.user_id,
                            self.conversation_id,
                            event_type,
                            json.dumps(_json_safe(payload), ensure_ascii=False),
                        ),
                    )
        except Exception:
            # Personalization telemetry must never block the user's main answer.
            return

    def _read_context(self, basic_profile: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(basic_profile or {})
        if not self.enabled:
            return context

        try:
            with postgres_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            p.profile_data,
                            pref.preferred_categories,
                            pref.disliked_ingredients,
                            pref.budget_min,
                            pref.budget_max,
                            pref.preferences_data
                        FROM user_profiles p
                        LEFT JOIN user_preferences pref ON pref.user_id = p.user_id
                        WHERE p.user_id::text = %s
                        LIMIT 1
                        """,
                        (self.user_id,),
                    )
                    profile_row = cursor.fetchone() or {}

                    cursor.execute(
                        """
                        SELECT result_data
                        FROM quiz_attempts
                        WHERE user_id::text = %s AND status = 'completed'
                        ORDER BY completed_at DESC NULLS LAST, started_at DESC
                        LIMIT 1
                        """,
                        (self.user_id,),
                    )
                    quiz_row = cursor.fetchone() or {}

                    cursor.execute(
                        """
                        SELECT COUNT(*) AS event_count
                        FROM user_behavior_events
                        WHERE user_id = %s
                        """,
                        (self.user_id,),
                    )
                    event_row = cursor.fetchone() or {}
        except Exception:
            return context

        profile_data = profile_row.get("profile_data") or {}
        preferences_data = profile_row.get("preferences_data") or {}
        quiz_data = quiz_row.get("result_data") or {}

        context.setdefault(
            "customer_type",
            profile_data.get("customerType")
            or profile_data.get("customer_type")
            or quiz_data.get("primary_type")
            or quiz_data.get("customerType"),
        )
        context.setdefault(
            "customer_type_label",
            profile_data.get("customerTypeLabel")
            or profile_data.get("customer_type_label")
            or quiz_data.get("primary_type_label")
            or quiz_data.get("primary_type"),
        )
        context["preferred_categories"] = _list(
            context.get("preferred_categories") or profile_row.get("preferred_categories")
        )
        context["avoid_ingredients"] = _list(
            context.get("avoid_ingredients") or profile_row.get("disliked_ingredients")
        )
        context["preferred_brands"] = _list(
            context.get("preferred_brands") or preferences_data.get("preferred_brands")
        )
        context["avoided_brands"] = _list(
            context.get("avoided_brands") or preferences_data.get("avoided_brands")
        )
        context["preferred_effects"] = _list(
            context.get("preferred_effects") or preferences_data.get("preferred_effects")
        )
        context["preferred_textures"] = _list(
            context.get("preferred_textures")
            or preferences_data.get("preferred_textures")
            or preferences_data.get("texture_preferences")
        )
        context["budget_min"] = context.get("budget_min") or profile_row.get("budget_min")
        context["budget_max"] = context.get("budget_max") or profile_row.get("budget_max")
        context["total_event_count"] = int(event_row.get("event_count") or 0)
        context["storage_backend"] = "postgres"
        return context

    def refresh(self, basic_profile: dict[str, Any] | None = None, *, analyze_if_needed: bool = False, force: bool = False) -> dict[str, Any]:
        context = self._read_context(basic_profile)
        if not self.enabled:
            return context
        try:
            with postgres_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO user_analysis_profiles (
                            user_id, profile, total_event_count, analyzed_at, updated_at
                        ) VALUES (%s, %s::jsonb, %s, NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            profile = EXCLUDED.profile,
                            total_event_count = EXCLUDED.total_event_count,
                            analyzed_at = NOW(),
                            updated_at = NOW()
                        """,
                        (
                            self.user_id,
                            json.dumps(_json_safe(context), ensure_ascii=False),
                            int(context.get("total_event_count") or 0),
                        ),
                    )
        except Exception:
            pass
        return context

    def record_intent(self, intent: Any, source_text: str = "") -> None:
        self._record(
            "intent",
            {"intent": _json_safe(intent), "source_text": str(source_text or "")[:1000]},
        )

    def record_rank_feedback(self, message: str, products: list[dict[str, Any]]) -> None:
        self._record(
            "rank_feedback",
            {"message": str(message or "")[:1000], "products": _json_safe(products[:20])},
        )

    @staticmethod
    def build_prompt_context(context: dict[str, Any]) -> str:
        if not context:
            return ""
        items: list[str] = []
        if context.get("customer_type"):
            items.append(
                f"消費人格：{context.get('customer_type_label') or context['customer_type']}"
            )
        if context.get("preferred_categories"):
            items.append("偏好品類：" + "、".join(context["preferred_categories"][:5]))
        if context.get("preferred_brands"):
            items.append("偏好品牌：" + "、".join(context["preferred_brands"][:5]))
        if context.get("avoided_brands"):
            items.append("避免品牌：" + "、".join(context["avoided_brands"][:5]))
        if context.get("preferred_effects"):
            items.append("偏好功效：" + "、".join(context["preferred_effects"][:5]))
        if context.get("avoid_ingredients"):
            items.append("避開成分：" + "、".join(context["avoid_ingredients"][:8]))
        if context.get("preferred_textures"):
            items.append("偏好質地：" + "、".join(context["preferred_textures"][:5]))
        budget_min = context.get("budget_min")
        budget_max = context.get("budget_max")
        if budget_min or budget_max:
            if budget_min and budget_max:
                budget_text = f"NT${budget_min}–{budget_max}"
            elif budget_max:
                budget_text = f"NT${budget_max} 以內"
            else:
                budget_text = f"NT${budget_min} 以上"
            items.append("常用預算：" + budget_text)
        if not items:
            return ""
        return "【個人化偏好】\n" + "；".join(items)
