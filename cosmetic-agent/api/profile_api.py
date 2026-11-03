from datetime import date, datetime
import os
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
)

class ProfileForm(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=100,
    )
    gender: str = ""
    birthday: str = ""
    email: Optional[EmailStr] = None
    lineBound: bool = False
    skinType: str = ""
    allergens: list[str] = Field(
        default_factory=list
    )
    otherAllergen: str = ""
    customerType: str = ""
    wellnessStatus: str = ""
    periodStartDate: str = ""
    periodEndDate: str = ""
    user_id: Optional[str] = None
    session_id: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(
        cls,
        value: str,
    ) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError("姓名不能為空")

        return normalized

    @field_validator("email", mode="before")
    @classmethod
    def normalize_optional_email(
        cls,
        value,
    ):
        """LINE Login 不一定提供 Email；空值代表尚未綁定。"""
        if value is None:
            return None

        normalized = str(value).strip()
        return normalized or None

    @field_validator("birthday")
    @classmethod
    def validate_birthday(
        cls,
        value: str,
    ) -> str:
        if not value:
            return ""

        try:
            birthday = datetime.strptime(
                value,
                "%Y-%m-%d",
            ).date()
        except ValueError as error:
            raise ValueError(
                "生日格式必須是 YYYY-MM-DD"
            ) from error

        today = date.today()

        # 計算五年前的日期，並處理 2 月 29 日
        try:
            five_years_ago = today.replace(
                year=today.year - 5
            )
        except ValueError:
            five_years_ago = today.replace(
                year=today.year - 5,
                day=28,
            )

        if birthday > five_years_ago:
            raise ValueError(
                "使用者年齡必須滿 5 歲"
            )

        return value

    @field_validator("periodStartDate", "periodEndDate")
    @classmethod
    def validate_optional_date(
        cls,
        value: str,
    ) -> str:
        if not value:
            return ""

        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(
                "日期格式必須是 YYYY-MM-DD"
            ) from error

        return value

def create_profile_router(
    get_agent: Callable,
    require_auth_session: Callable | None = None,
) -> APIRouter:
    """
    Profile API router 工廠（api_server 以 create_profile_router(get_agent) 掛載）
    - PUT /api/user/profile：驗證(ProfileForm) → 存 user_profiles → 同步 Global Memory
    - GET /api/v1/profile：讀取個人檔案
    """
    router = APIRouter()

    _SKINTYPE_MAP = {"油性肌": "oily", "乾性肌": "dry", "中性肌": "normal",
                     "混合肌": "combination", "敏感肌": "sensitive"}

    def repository():
        if os.getenv("AUTH_STORAGE_BACKEND", "neo4j").strip().lower() == "postgres":
            from database.postgres.repositories.profile import (
                postgres_profile_repository,
            )

            return postgres_profile_repository
        from database.profile_repository import profile_repository

        return profile_repository

    def current_user_id(request: Request, fallback: str = "") -> str:
        if require_auth_session is not None:
            return str(require_auth_session(request)["user_id"])
        return fallback or "anonymous"

    @router.put("/api/user/profile")
    def save_profile(form: ProfileForm, request: Request):
        uid = current_user_id(request, form.user_id or form.session_id or "")
        try:
            saved_profile = repository().save_profile(
                user_id=uid,
                profile=form.model_dump(),
            )
        except Exception as error:
            from database.postgres.repositories.profile import (
                ProfileEmailMismatchError,
                ProfileNotFoundError,
            )

            if isinstance(error, ProfileEmailMismatchError):
                raise HTTPException(status_code=409, detail=str(error)) from error
            if isinstance(error, ProfileNotFoundError):
                raise HTTPException(status_code=404, detail=str(error)) from error
            raise
        

        # 重新從主資料庫載入 Profile，並同步到 Agent Memory。
        # 即使使用者清空膚質或過敏原，也必須覆蓋舊的 Zep 值。
        try:
            agent = get_agent(uid)
            if hasattr(agent, "refresh_profile_context"):
                agent.refresh_profile_context()
            else:
                agent.memory.set(
                    "skin_type",
                    _SKINTYPE_MAP.get(form.skinType, form.skinType) or None,
                )
                allergens = list(form.allergens)
                if form.otherAllergen:
                    allergens.append(form.otherAllergen)
                agent.memory.set("disliked_ingredients", allergens)
            agent.memory.save()
        except Exception as e:
            print(f"[profile_api] memory 同步失敗（不影響存檔）：{e}")
     
        return {
            "ok": True,
            "profile": saved_profile,
        }


    @router.get("/api/v1/profile")
    def get_profile(
        request: Request,
        user_id: str = "anonymous",
    ):
        uid = current_user_id(request, user_id)
        profile = repository().get_profile(user_id=uid)
    
        return {
            "ok": True,
            "profile": profile or {},
        }

    return router
