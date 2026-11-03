"""Storage-independent OAuth identity write contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


class OAuthIdentityError(RuntimeError):
    """Base exception for OAuth identity resolution."""


class OAuthIdentityValidationError(OAuthIdentityError):
    """Raised when provider data is incomplete or unsupported."""


class OAuthIdentityConflictError(OAuthIdentityError):
    """Raised when verified provider data points at different accounts."""


@dataclass(frozen=True, slots=True)
class OAuthIdentityInput:
    provider: Literal["google", "line"]
    provider_subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthIdentityResolution:
    user_id: UUID
    username: str
    account_status: str
    created_user: bool
    linked_identity: bool
    matched_by: Literal["provider_identity", "verified_email", "new_user"]


class OAuthIdentityRepository(Protocol):
    def resolve_login(self, identity: OAuthIdentityInput) -> OAuthIdentityResolution:
        """Atomically resolve or create the canonical account for an OAuth login."""
