"""Small value types shared by reconstructed registration modules (added 2026-10-21)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RegisteredAccount:
    user_id: UUID
    username: str
    email: str
