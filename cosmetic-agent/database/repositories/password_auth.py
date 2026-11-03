"""Storage-independent password authentication contract.

RECONSTRUCTED 2026-10-21: the original file was excluded from the source export
because a heuristic secret scanner flagged it. The contract below is derived from
its call sites (tools/auth/postgres_password_login.py, api/api_server.py and
database/postgres/repositories/password_reset.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class PasswordAuthError(RuntimeError):
    """Base class for password repository failures."""


# Backwards-compatible alias used by the reconstructed modules.
PasswordAuthRepositoryError = PasswordAuthError


class PasswordAuthValidationError(PasswordAuthRepositoryError, ValueError):
    """Raised when a username, email or password violates account rules."""


class InvalidCredentialsError(PasswordAuthRepositoryError):
    """Raised for unknown usernames and wrong passwords without distinguishing them."""


class AccountLockedError(PasswordAuthRepositoryError):
    """Raised while an account is temporarily locked after failed attempts."""


class PasswordNotSetError(PasswordAuthRepositoryError):
    """Raised when an OAuth-only account has no password credential."""


class PasswordAlreadySetError(PasswordAuthRepositoryError):
    """Raised when an initial password is requested for an account that has one."""


class EmailUnavailableError(PasswordAuthRepositoryError):
    """Raised when a normalized email already belongs to another account."""


class UsernameUnavailableError(PasswordAuthRepositoryError):
    """Raised when a normalized username already belongs to another account."""


@dataclass(frozen=True, slots=True)
class PasswordAccount:
    user_id: UUID
    username: str


class PasswordAuthRepository(Protocol):
    def register(
        self,
        *,
        username: str,
        verified_email: str,
        password: str,
    ) -> PasswordAccount:
        """Create one active account owning a verified email and a password."""

    def authenticate(self, *, username: str, password: str) -> PasswordAccount:
        """Return the account for valid credentials or raise a repository error."""

    def set_initial_password(self, *, user_id: UUID, new_password: str) -> None:
        """Add a password to an OAuth-only account."""

    def change_password(
        self,
        *,
        user_id: UUID,
        current_password: str,
        new_password: str,
    ) -> None:
        """Replace a password after verifying the current one."""