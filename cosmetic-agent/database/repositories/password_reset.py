"""Storage-independent password reset challenge contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class PasswordResetError(RuntimeError):
    """Base exception for password reset operations."""


class PasswordResetUnavailableError(PasswordResetError):
    """Raised without revealing whether an email belongs to an account."""


class PasswordResetRateLimitedError(PasswordResetError):
    """Raised when too many reset messages were requested."""


class InvalidPasswordResetCodeError(PasswordResetError):
    """Raised when a reset challenge is invalid, expired, consumed, or exhausted."""


@dataclass(frozen=True, slots=True)
class PasswordResetDispatch:
    challenge_id: UUID
    email: str
    code: str
    expires_in_seconds: int


class PasswordResetRepository(Protocol):
    def issue(self, *, email: str) -> PasswordResetDispatch:
        """Create a challenge and return the one-time code for the mail boundary."""

    def consume(
        self,
        *,
        challenge_id: UUID,
        code: str,
        new_password: str,
    ) -> UUID:
        """Consume one valid challenge and replace or create the member password."""

    def invalidate(self, *, challenge_id: UUID) -> None:
        """Make a challenge unusable, for example when message delivery fails."""
