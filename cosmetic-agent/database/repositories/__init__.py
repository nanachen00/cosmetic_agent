"""Storage-independent repository contracts."""

from .identity import AccountIdentity, IdentityRepository
from .oauth_identity import (
    OAuthIdentityConflictError,
    OAuthIdentityInput,
    OAuthIdentityRepository,
    OAuthIdentityResolution,
    OAuthIdentityValidationError,
)
from .password_auth import (
    AccountLockedError,
    EmailUnavailableError,
    InvalidCredentialsError,
    PasswordAccount,
    PasswordAlreadySetError,
    PasswordAuthError,
    PasswordAuthRepository,
    PasswordAuthValidationError,
    PasswordNotSetError,
    UsernameUnavailableError,
)

__all__ = [
    "AccountIdentity",
    "IdentityRepository",
    "OAuthIdentityConflictError",
    "OAuthIdentityInput",
    "OAuthIdentityRepository",
    "OAuthIdentityResolution",
    "OAuthIdentityValidationError",
    "AccountLockedError",
    "EmailUnavailableError",
    "InvalidCredentialsError",
    "PasswordAccount",
    "PasswordAlreadySetError",
    "PasswordAuthError",
    "PasswordAuthRepository",
    "PasswordAuthValidationError",
    "PasswordNotSetError",
    "UsernameUnavailableError",
]
