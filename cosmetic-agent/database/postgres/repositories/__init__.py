"""PostgreSQL repository implementations."""

from .identity import PostgresIdentityRepository
from .oauth_identity import PostgresOAuthIdentityRepository
from .password_auth import PostgresPasswordAuthRepository

__all__ = [
    "PostgresIdentityRepository",
    "PostgresOAuthIdentityRepository",
    "PostgresPasswordAuthRepository",
]
