"""PostgreSQL profile storage keyed by the canonical user UUID."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from database.postgres.connection import postgres_jsonb

from database.postgres.connection import postgres_connection


class ProfileNotFoundError(RuntimeError):
    pass


class ProfileEmailMismatchError(RuntimeError):
    pass


class PostgresProfileRepository:
    @staticmethod
    def _uuid(user_id: str) -> UUID:
        try:
            return UUID(str(user_id))
        except (TypeError, ValueError) as error:
            raise ProfileNotFoundError("找不到會員資料，請重新登入") from error

    @staticmethod
    def _allergens(profile: dict[str, Any]) -> list[str]:
        values = list(profile.get("allergens", []))
        other = str(profile.get("otherAllergen", "")).strip()
        if other:
            values.append(other)
        return list(
            dict.fromkeys(
                item.strip()
                for item in values
                if isinstance(item, str) and item.strip()
            )
        )

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        canonical_user_id = self._uuid(user_id)
        submitted_email = str(profile.get("email") or "").strip().lower()
        birthday_value = profile.get("birthday") or None
        birthday = date.fromisoformat(birthday_value) if birthday_value else None
        allergens = self._allergens(profile)
        profile_data = {
            "skinType": str(profile.get("skinType", "")),
            "allergens": allergens,
            "customerType": str(profile.get("customerType", "")),
            "wellnessStatus": str(profile.get("wellnessStatus", "")),
            "periodStartDate": str(profile.get("periodStartDate", "")),
            "periodEndDate": str(profile.get("periodEndDate", "")),
        }
        with postgres_connection() as connection:
            identity = connection.execute(
                """
                SELECT ue.email,
                       COALESCE(array_agg(ai.provider) FILTER (
                           WHERE ai.provider IS NOT NULL
                       ), ARRAY[]::text[]) AS providers
                FROM users AS u
                LEFT JOIN LATERAL (
                    SELECT email FROM user_emails
                    WHERE user_id = u.id AND verified_at IS NOT NULL
                    ORDER BY is_primary DESC, created_at ASC LIMIT 1
                ) AS ue ON true
                LEFT JOIN auth_identities AS ai ON ai.user_id = u.id
                WHERE u.id = %s AND u.deleted_at IS NULL
                GROUP BY u.id, ue.email
                """,
                (canonical_user_id,),
            ).fetchone()
            if identity is None:
                raise ProfileNotFoundError("找不到會員資料，請重新登入")
            verified_email = identity["email"] or ""
            if submitted_email and submitted_email != verified_email:
                raise ProfileEmailMismatchError(
                    "Email 尚未驗證，請先完成 Email 驗證再更新"
                )
            connection.execute(
                """
                INSERT INTO user_profiles (
                    user_id, display_name, gender, birthday, profile_data
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    gender = EXCLUDED.gender,
                    birthday = EXCLUDED.birthday,
                    profile_data = EXCLUDED.profile_data,
                    updated_at = now()
                """,
                (
                    canonical_user_id,
                    str(profile.get("name", "")).strip(),
                    str(profile.get("gender", "")),
                    birthday,
                    postgres_jsonb(profile_data),
                ),
            )
        return self.get_profile(user_id) or {}

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        canonical_user_id = self._uuid(user_id)
        with postgres_connection() as connection:
            row = connection.execute(
                """
                SELECT p.display_name, p.gender, p.birthday, p.profile_data,
                       email.email,
                       EXISTS (
                           SELECT 1 FROM auth_identities AS linked
                           WHERE linked.user_id = u.id AND linked.provider = 'line'
                       ) AS line_bound
                FROM users AS u
                LEFT JOIN user_profiles AS p ON p.user_id = u.id
                LEFT JOIN LATERAL (
                    SELECT ue.email FROM user_emails AS ue
                    WHERE ue.user_id = u.id AND ue.verified_at IS NOT NULL
                    ORDER BY ue.is_primary DESC, ue.created_at ASC LIMIT 1
                ) AS email ON true
                WHERE u.id = %s AND u.deleted_at IS NULL
                """,
                (canonical_user_id,),
            ).fetchone()
        if row is None:
            return None
        profile_data = row["profile_data"] or {}
        return {
            "user_id": str(canonical_user_id),
            "name": row["display_name"] or "",
            "gender": row["gender"] or "",
            "birthday": row["birthday"].isoformat() if row["birthday"] else "",
            "email": row["email"] or "",
            "lineBound": bool(row["line_bound"]),
            "skinType": profile_data.get("skinType", ""),
            "allergens": list(profile_data.get("allergens", [])),
            "otherAllergen": "",
            "customerType": profile_data.get("customerType", ""),
            "wellnessStatus": profile_data.get("wellnessStatus", ""),
            "periodStartDate": profile_data.get("periodStartDate", ""),
            "periodEndDate": profile_data.get("periodEndDate", ""),
        }

    def update_skin_profile(
        self,
        user_id: str,
        skin_type: str | None = None,
        allergens: list[str] | None = None,
    ) -> dict[str, Any]:
        """Partially update stable skin facts without replacing other JSON data."""
        canonical_user_id = self._uuid(user_id)
        patch: dict[str, Any] = {}
        if skin_type is not None:
            patch["skinType"] = str(skin_type)
        if allergens is not None:
            patch["allergens"] = list(
                dict.fromkeys(
                    item.strip()
                    for item in allergens
                    if isinstance(item, str) and item.strip()
                )
            )

        if not patch:
            return self.get_profile(user_id) or {}

        with postgres_connection() as connection:
            identity = connection.execute(
                """
                SELECT 1
                FROM users
                WHERE id = %s AND deleted_at IS NULL
                """,
                (canonical_user_id,),
            ).fetchone()
            if identity is None:
                raise ProfileNotFoundError("找不到會員資料，請重新登入")

            connection.execute(
                """
                INSERT INTO user_profiles (user_id, profile_data)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    profile_data = COALESCE(user_profiles.profile_data, '{}'::jsonb)
                        || EXCLUDED.profile_data,
                    updated_at = now()
                """,
                (canonical_user_id, postgres_jsonb(patch)),
            )

        return self.get_profile(user_id) or {}

    def update_customer_type(
        self,
        user_id: str,
        customer_type: str,
    ) -> dict[str, Any]:
        """Mirror the latest completed customer quiz into profile_data."""
        canonical_user_id = self._uuid(user_id)
        with postgres_connection() as connection:
            identity = connection.execute(
                """
                SELECT 1
                FROM users
                WHERE id = %s AND deleted_at IS NULL
                """,
                (canonical_user_id,),
            ).fetchone()
            if identity is None:
                raise ProfileNotFoundError("找不到會員資料，請重新登入")

            connection.execute(
                """
                INSERT INTO user_profiles (user_id, profile_data)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    profile_data = COALESCE(user_profiles.profile_data, '{}'::jsonb)
                        || EXCLUDED.profile_data,
                    updated_at = now()
                """,
                (
                    canonical_user_id,
                    postgres_jsonb({"customerType": str(customer_type or "").strip()}),
                ),
            )
        return self.get_profile(user_id) or {}


postgres_profile_repository = PostgresProfileRepository()
