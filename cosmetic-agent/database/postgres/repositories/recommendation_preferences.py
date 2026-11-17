"""Canonical PostgreSQL storage for user-confirmed recommendation preferences."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_connection, postgres_jsonb


class RecommendationPreferenceUserNotFound(RuntimeError):
    pass


class RecommendationPreferenceConflict(RuntimeError):
    pass


_MANAGED_KEYS = (
    "preferred_brands",
    "avoided_brands",
    "preferred_textures",
    "texture_preferences",
)


def _uuid(value: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise RecommendationPreferenceUserNotFound(
            "找不到會員資料，請重新登入"
        ) from error


def _brand_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", normalized)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        key = _brand_key(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _number(value: Any) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class RecommendationPreferenceRepository:
    """Patch only explicitly managed fields and preserve unrelated preferences."""

    @staticmethod
    def _shape(row: dict[str, Any]) -> dict[str, Any]:
        data = dict(row.get("preferences_data") or {})
        if "preferred_textures" in data:
            textures = data.get("preferred_textures")
        else:
            textures = data.get("texture_preferences")
        texture_values = _strings(textures)
        preferred_texture = next(
            (value for value in texture_values if value in {"light", "rich"}),
            None,
        )
        return {
            "budget_max": _number(row.get("budget_max")),
            "preferred_texture": preferred_texture,
            "preferred_brands": _strings(data.get("preferred_brands")),
            "avoided_brands": _strings(data.get("avoided_brands")),
            "updated_at": _timestamp(row.get("updated_at")),
        }

    @staticmethod
    def _locked_row(connection: Any, user_id: UUID) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT pref.budget_min, pref.budget_max,
                   pref.preferences_data, pref.updated_at
            FROM users AS u
            LEFT JOIN user_preferences AS pref ON pref.user_id = u.id
            WHERE u.id = %s AND u.deleted_at IS NULL
            FOR UPDATE OF u
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            raise RecommendationPreferenceUserNotFound(
                "找不到會員資料，請重新登入"
            )
        return dict(row)

    def get(self, user_id: str) -> dict[str, Any]:
        canonical = _uuid(user_id)
        with postgres_connection() as connection:
            row = connection.execute(
                """
                SELECT pref.budget_max, pref.preferences_data, pref.updated_at
                FROM users AS u
                LEFT JOIN user_preferences AS pref ON pref.user_id = u.id
                WHERE u.id = %s AND u.deleted_at IS NULL
                """,
                (canonical,),
            ).fetchone()
        if row is None:
            raise RecommendationPreferenceUserNotFound(
                "找不到會員資料，請重新登入"
            )
        return self._shape(dict(row))

    def patch(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        canonical = _uuid(user_id)
        with postgres_connection() as connection:
            current = self._locked_row(connection, canonical)
            data = dict(current.get("preferences_data") or {})
            budget_max = current.get("budget_max")
            budget_min = current.get("budget_min")

            if "budget_max" in changes:
                budget_max = changes["budget_max"]
                # The public preference editor owns a maximum, not a hidden range.
                budget_min = None
            if "preferred_texture" in changes:
                texture = changes["preferred_texture"]
                data.pop("texture_preferences", None)
                data["preferred_textures"] = [texture] if texture else []
            for key in ("preferred_brands", "avoided_brands"):
                if key in changes:
                    data[key] = list(changes[key] or [])

            preferred = _strings(data.get("preferred_brands"))
            avoided = _strings(data.get("avoided_brands"))
            overlap = {_brand_key(value) for value in preferred} & {
                _brand_key(value) for value in avoided
            }
            if overlap:
                raise RecommendationPreferenceConflict(
                    "同一品牌不能同時設為喜歡與避用"
                )

            row = connection.execute(
                """
                INSERT INTO user_preferences (
                    user_id, budget_min, budget_max, preferences_data, updated_at
                ) VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    budget_min = EXCLUDED.budget_min,
                    budget_max = EXCLUDED.budget_max,
                    preferences_data = EXCLUDED.preferences_data,
                    updated_at = NOW()
                RETURNING budget_max, preferences_data, updated_at
                """,
                (
                    canonical,
                    budget_min,
                    budget_max,
                    postgres_jsonb(data),
                ),
            ).fetchone()
        return self._shape(dict(row))

    def reset(self, user_id: str) -> dict[str, Any]:
        canonical = _uuid(user_id)
        with postgres_connection() as connection:
            current = self._locked_row(connection, canonical)
            data = dict(current.get("preferences_data") or {})
            for key in _MANAGED_KEYS:
                data.pop(key, None)
            row = connection.execute(
                """
                INSERT INTO user_preferences (
                    user_id, budget_min, budget_max, preferences_data, updated_at
                ) VALUES (%s, NULL, NULL, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    budget_min = NULL,
                    budget_max = NULL,
                    preferences_data = EXCLUDED.preferences_data,
                    updated_at = NOW()
                RETURNING budget_max, preferences_data, updated_at
                """,
                (canonical, postgres_jsonb(data)),
            ).fetchone()
        return self._shape(dict(row))


recommendation_preference_repository = RecommendationPreferenceRepository()
