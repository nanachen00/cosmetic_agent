"""Storage-independent verified-email registration contract."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class EmailRegistrationError(RuntimeError):
    pass


class EmailRegistrationRateLimitedError(EmailRegistrationError):
    pass


class InvalidEmailRegistrationCodeError(EmailRegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class EmailRegistrationDispatch:
    challenge_id: UUID
    email: str
    code: str
    expires_in_seconds: int
