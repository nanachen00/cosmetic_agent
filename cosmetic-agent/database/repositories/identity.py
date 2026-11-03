"""Repository contract for canonical BLINK AI account identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """Database-neutral account identity returned to application services."""

    user_id: UUID
    status: str
    username: str | None
    primary_email: str | None
    email_verified: bool
    providers: tuple[str, ...]
    has_password: bool
    display_name: str | None = None
    username_changed_at: datetime | None = None


class IdentityRepository(Protocol):
    """Read contract used while relational identity storage is introduced."""

    def get_by_user_id(self, user_id: UUID | str) -> AccountIdentity | None:
        """Return one canonical account by immutable user id."""

    def get_by_email(self, email: str) -> AccountIdentity | None:
        """Return the account that owns a normalized email."""

    def is_username_available(self, username: str) -> bool:
        """Return whether a normalized username is not owned by any account."""
