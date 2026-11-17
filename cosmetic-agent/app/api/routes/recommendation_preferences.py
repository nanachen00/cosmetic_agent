"""Authenticated API for explicit, user-controlled recommendation preferences."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database.postgres.repositories.recommendation_preferences import (
    RecommendationPreferenceConflict,
    RecommendationPreferenceRepository,
    RecommendationPreferenceUserNotFound,
    recommendation_preference_repository,
)


def _brand_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", normalized)


def _brands(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("品牌偏好必須是陣列或 null")
    if len(value) > 10:
        raise ValueError("品牌偏好最多 10 個")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("品牌名稱必須是文字")
        text = " ".join(unicodedata.normalize("NFKC", item).split())
        if not text:
            continue
        if len(text) > 40:
            raise ValueError("品牌名稱最多 40 字")
        key = _brand_key(text)
        if not key:
            raise ValueError("品牌名稱格式無效")
        if key not in seen:
            seen.add(key)
            result.append(text)
    return result


class RecommendationPreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_max: float | None = Field(
        default=None,
        gt=0,
        le=100_000,
        allow_inf_nan=False,
    )
    preferred_texture: Literal["light", "rich"] | None = None
    preferred_brands: list[str] | None = None
    avoided_brands: list[str] | None = None

    @field_validator("budget_max", mode="before")
    @classmethod
    def reject_boolean_budget(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("預算必須是數字")
        return value

    @field_validator("preferred_brands", "avoided_brands", mode="before")
    @classmethod
    def normalize_brands(cls, value: Any) -> list[str] | None:
        return _brands(value)

    @model_validator(mode="after")
    def no_brand_overlap(self):
        if self.preferred_brands is not None and len(self.preferred_brands) > 5:
            raise ValueError("喜歡品牌最多 5 個")
        if self.preferred_brands is None or self.avoided_brands is None:
            return self
        preferred = {_brand_key(value) for value in self.preferred_brands}
        avoided = {_brand_key(value) for value in self.avoided_brands}
        if preferred & avoided:
            raise ValueError("同一品牌不能同時設為喜歡與避用")
        return self


def create_recommendation_preferences_router(
    *,
    require_auth_session: Callable[[Request], dict[str, Any]],
    repository: RecommendationPreferenceRepository = (
        recommendation_preference_repository
    ),
) -> APIRouter:
    router = APIRouter(
        prefix="/api/user/preferences",
        tags=["recommendation-preferences"],
    )

    def user_id(request: Request) -> str:
        session = require_auth_session(request)
        value = str(session.get("user_id") or "").strip()
        if not value:
            raise HTTPException(status_code=401, detail="Authentication required")
        return value

    def response(preferences: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "preferences": preferences}

    def not_found(error: RecommendationPreferenceUserNotFound) -> HTTPException:
        return HTTPException(status_code=404, detail=str(error))

    @router.get("")
    def get_preferences(request: Request) -> dict[str, Any]:
        try:
            return response(repository.get(user_id(request)))
        except RecommendationPreferenceUserNotFound as error:
            raise not_found(error) from error

    @router.patch("")
    def patch_preferences(
        payload: RecommendationPreferencePatch,
        request: Request,
    ) -> dict[str, Any]:
        changes = payload.model_dump(exclude_unset=True)
        try:
            owner = user_id(request)
            preferences = (
                repository.patch(owner, changes)
                if changes
                else repository.get(owner)
            )
            return response(preferences)
        except RecommendationPreferenceUserNotFound as error:
            raise not_found(error) from error
        except RecommendationPreferenceConflict as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.delete("")
    def reset_preferences(request: Request) -> dict[str, Any]:
        try:
            return response(repository.reset(user_id(request)))
        except RecommendationPreferenceUserNotFound as error:
            raise not_found(error) from error

    return router
