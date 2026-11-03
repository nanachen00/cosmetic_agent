from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import smtplib
import ssl
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any


CODE_TTL_MINUTES = 10
MAX_SENDS_PER_WINDOW = 3
SEND_WINDOW_MINUTES = 30
MAX_CODE_ATTEMPTS = 5


class PasswordResetError(Exception):
    pass


class PasswordResetNotConfiguredError(PasswordResetError):
    pass


class VerifiedEmailRequiredError(PasswordResetError):
    pass


class PasswordResetRateLimitedError(PasswordResetError):
    pass


class InvalidPasswordResetCodeError(PasswordResetError):
    pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PasswordResetNotConfiguredError("信箱驗證服務尚未設定完成")
    return value


def _hash_code(challenge_id: str, code: str) -> str:
    secret = _required_env("PASSWORD_RESET_SECRET").encode("utf-8")
    return hmac.new(
        secret,
        f"{challenge_id}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _mask_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return ""
    shown = local[:2] if len(local) > 2 else local[:1]
    return f"{shown}{'*' * max(2, len(local) - len(shown))}@{domain}"


def _send_code(email: str, code: str) -> None:
    host = _required_env("SMTP_HOST")
    username = _required_env("SMTP_USERNAME")
    password = _required_env("SMTP_PASSWORD")
    sender = _required_env("SMTP_FROM_EMAIL")
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    message = EmailMessage()
    message["Subject"] = "BLINK AI 會員密碼驗證碼"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "你的 BLINK AI 驗證碼是："
        f"{code}\n\n驗證碼 {CODE_TTL_MINUTES} 分鐘內有效，且只能使用一次。"
        "\n若不是你本人操作，請忽略此郵件。"
    )

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=15) as server:
        if use_tls:
            server.starttls(context=context)
        server.login(username, password)
        server.send_message(message)


class PasswordResetService:
    def __init__(self, driver: Any):
        self.driver = driver
        self._constraints_ready = False
        self._constraints_lock = threading.Lock()

    def _ensure_constraints(self) -> None:
        if self._constraints_ready:
            return
        with self._constraints_lock:
            if self._constraints_ready:
                return
            with self.driver.session() as session:
                session.run(
                    """
                    CREATE CONSTRAINT password_reset_challenge_id_unique IF NOT EXISTS
                    FOR (challenge:PasswordResetChallenge)
                    REQUIRE challenge.challenge_id IS UNIQUE
                    """
                ).consume()
            self._constraints_ready = True

    def request_code(self, *, user_id: str) -> dict[str, str]:
        _required_env("PASSWORD_RESET_SECRET")
        _required_env("SMTP_HOST")
        _required_env("SMTP_USERNAME")
        _required_env("SMTP_PASSWORD")
        _required_env("SMTP_FROM_EMAIL")
        self._ensure_constraints()

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=SEND_WINDOW_MINUTES)
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (user:User {user_id: $user_id})
                OPTIONAL MATCH (user)-[:HAS_PASSWORD_RESET]->(
                    recent:PasswordResetChallenge
                )
                WHERE recent.created_at >= datetime($window_start)
                RETURN user.email_normalized AS email,
                       coalesce(user.email_verified, false) AS email_verified,
                       count(recent) AS recent_count
                """,
                user_id=user_id,
                window_start=window_start.isoformat(),
            ).single()
        if record is None or not record["email_verified"] or not record["email"]:
            raise VerifiedEmailRequiredError("請先完成 Email 驗證")
        if int(record["recent_count"] or 0) >= MAX_SENDS_PER_WINDOW:
            raise PasswordResetRateLimitedError("驗證碼寄送次數過多，請稍後再試")

        email = str(record["email"])
        challenge_id = uuid.uuid4().hex
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = _hash_code(challenge_id, code)
        expires_at = now + timedelta(minutes=CODE_TTL_MINUTES)

        with self.driver.session() as session:
            session.run(
                """
                MATCH (user:User {user_id: $user_id})
                CREATE (challenge:PasswordResetChallenge {
                    challenge_id: $challenge_id,
                    code_hash: $code_hash,
                    attempts_remaining: $attempts,
                    created_at: datetime($now),
                    expires_at: datetime($expires_at)
                })
                CREATE (user)-[:HAS_PASSWORD_RESET]->(challenge)
                """,
                user_id=user_id,
                challenge_id=challenge_id,
                code_hash=code_hash,
                attempts=MAX_CODE_ATTEMPTS,
                now=now.isoformat(),
                expires_at=expires_at.isoformat(),
            ).consume()

        try:
            _send_code(email, code)
        except Exception:
            with self.driver.session() as session:
                session.run(
                    """
                    MATCH (challenge:PasswordResetChallenge {
                        challenge_id: $challenge_id
                    })
                    DETACH DELETE challenge
                    """,
                    challenge_id=challenge_id,
                ).consume()
            raise PasswordResetError("驗證信寄送失敗，請稍後再試")

        return {
            "challenge_id": challenge_id,
            "masked_email": _mask_email(email),
        }

    def consume_code(
        self,
        *,
        user_id: str,
        challenge_id: str,
        code: str,
    ) -> None:
        self._ensure_constraints()
        now = datetime.now(timezone.utc)
        with self.driver.session() as session:
            record = session.run(
                """
                MATCH (user:User {user_id: $user_id})-[:HAS_PASSWORD_RESET]->(
                    challenge:PasswordResetChallenge {
                        challenge_id: $challenge_id
                    }
                )
                WHERE challenge.used_at IS NULL
                  AND challenge.expires_at > datetime($now)
                  AND challenge.attempts_remaining > 0
                RETURN challenge.code_hash AS code_hash,
                       challenge.attempts_remaining AS attempts_remaining
                """,
                user_id=user_id,
                challenge_id=challenge_id,
                now=now.isoformat(),
            ).single()
        if record is None:
            raise InvalidPasswordResetCodeError("驗證碼已失效，請重新取得")

        supplied_hash = _hash_code(challenge_id, (code or "").strip())
        if not hmac.compare_digest(supplied_hash, record["code_hash"]):
            with self.driver.session() as session:
                session.run(
                    """
                    MATCH (challenge:PasswordResetChallenge {
                        challenge_id: $challenge_id
                    })
                    SET challenge.attempts_remaining =
                        challenge.attempts_remaining - 1
                    """,
                    challenge_id=challenge_id,
                ).consume()
            raise InvalidPasswordResetCodeError("驗證碼不正確")

        with self.driver.session() as session:
            updated = session.run(
                """
                MATCH (user:User {user_id: $user_id})-[:HAS_PASSWORD_RESET]->(
                    challenge:PasswordResetChallenge {
                        challenge_id: $challenge_id
                    }
                )
                WHERE challenge.used_at IS NULL
                  AND challenge.expires_at > datetime($now)
                  AND challenge.attempts_remaining > 0
                SET challenge.used_at = datetime($now),
                    challenge.attempts_remaining = 0
                RETURN challenge.challenge_id AS challenge_id
                """,
                user_id=user_id,
                challenge_id=challenge_id,
                now=now.isoformat(),
            ).single()
        if updated is None:
            raise InvalidPasswordResetCodeError("驗證碼已被使用")


_service_instance: PasswordResetService | None = None
_service_lock = threading.Lock()


def create_password_reset_service() -> PasswordResetService:
    global _service_instance
    if _service_instance is not None:
        return _service_instance
    with _service_lock:
        if _service_instance is None:
            from database.profile_repository import profile_repository

            _service_instance = PasswordResetService(profile_repository.neo4j_driver)
    return _service_instance
