"""Explicit storage switches used during the PostgreSQL migration."""

from __future__ import annotations

import os
from typing import Literal, cast

IdentityReadStore = Literal["neo4j", "postgres"]
_ALLOWED_IDENTITY_READ_STORES = {"neo4j", "postgres"}


class StorageConfigurationError(RuntimeError):
    """Raised when a storage feature flag has an unsupported value."""


def identity_read_store() -> IdentityReadStore:
    """Return the configured identity read store; legacy Neo4j is the safe default."""

    value = os.getenv("BLINKAI_IDENTITY_READ_STORE", "neo4j").strip().lower()
    if value not in _ALLOWED_IDENTITY_READ_STORES:
        allowed = ", ".join(sorted(_ALLOWED_IDENTITY_READ_STORES))
        raise StorageConfigurationError(
            f"BLINKAI_IDENTITY_READ_STORE 必須是以下其中之一：{allowed}"
        )
    return cast(IdentityReadStore, value)


def postgres_identity_reads_enabled() -> bool:
    """Return true only after PostgreSQL identity reads are explicitly enabled."""

    return identity_read_store() == "postgres"
