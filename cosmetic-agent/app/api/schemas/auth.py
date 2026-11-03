"""Request bodies for /api/auth routes.

RECONSTRUCTED 2026-10-21. Field names match the live frontend
(frontend/lib/account-api.ts and app/login/setup-username).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PasswordRegisterRequest(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)


class PasswordLoginRequest(BaseModel):
    username: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=256)


class VerifyPasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=256)


class ConfirmPasswordResetRequest(BaseModel):
    challenge_id: str = Field(..., max_length=64)
    code: str = Field(..., max_length=16)
    new_password: str = Field(..., max_length=256)


class UsernameClaimRequest(BaseModel):
    username: str = Field(..., max_length=64)


class ChangeUsernameRequest(BaseModel):
    username: str = Field(..., max_length=64)
