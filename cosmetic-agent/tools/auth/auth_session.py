from __future__ import annotations

import hashlib
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any


SESSION_COOKIE_NAME = (
    os.getenv("AUTH_SESSION_COOKIE_NAME", "blink_session").strip()
    or "blink_session"
)
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
RECENT_AUTH_SECONDS = 10 * 60


class AuthSessionError(Exception):
    """Base exception for authenticated browser sessions."""


class InvalidSessionError(AuthSessionError):
    pass


class RecentAuthenticationRequiredError(AuthSessionError):
    pass


def create_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def session_cookie_secure(hostname: str = "") -> bool:
    return (hostname or "").lower() not in {"", "localhost", "127.0.0.1"}


def session_cookie_options(hostname: str = "") -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": session_cookie_secure(hostname),
        "samesite": "lax",
        "max_age": SESSION_TTL_SECONDS,
        "path": "/",
    }


class AuthSessionService:
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
                    CREATE CONSTRAINT auth_session_token_hash_unique IF NOT EXISTS
                    FOR (auth_session:AuthSession)
                    REQUIRE auth_session.token_hash IS UNIQUE
                    """
                ).consume()
            self._constraints_ready = True

    def create(
        self,
        *,
        user_id: str,
        auth_method: str,
        client_ip: str = "",
        user_agent: str = "",
        now: datetime | None = None,
    ) -> str:
        if not user_id or not auth_method:
            raise AuthSessionError("無法建立登入狀態")

        self._ensure_constraints()
        issued_at = now or datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(seconds=SESSION_TTL_SECONDS)
        token = create_session_token()
        query = """
        MATCH (user:User {user_id: $user_id})
        CREATE (auth_session:AuthSession {
            token_hash: $token_hash,
            auth_method: $auth_method,
            authenticated_at: datetime($issued_at),
            created_at: datetime($issued_at),
            last_seen_at: datetime($issued_at),
            expires_at: datetime($expires_at),
            client_ip: $client_ip,
            user_agent: $user_agent
        })
        CREATE (user)-[:HAS_AUTH_SESSION]->(auth_session)
        RETURN user.user_id AS user_id
        """
        with self.driver.session() as session:
            record = session.run(
                query,
                user_id=user_id,
                token_hash=hash_session_token(token),
                auth_method=auth_method,
                issued_at=issued_at.isoformat(),
                expires_at=expires_at.isoformat(),
                client_ip=(client_ip or "")[:128],
                user_agent=(user_agent or "")[:512],
            ).single()
        if record is None:
            raise AuthSessionError("找不到會員資料，無法建立登入狀態")
        return token

    def authenticate(
        self,
        token: str,
        *,
        require_recent: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not token:
            raise InvalidSessionError("請先登入")

        self._ensure_constraints()
        current_time = now or datetime.now(timezone.utc)
        recent_after = current_time - timedelta(seconds=RECENT_AUTH_SECONDS)
        query = """
        MATCH (user:User)-[:HAS_AUTH_SESSION]->(
            auth_session:AuthSession {token_hash: $token_hash}
        )
        WHERE auth_session.expires_at > datetime($now)
        SET auth_session.last_seen_at = datetime($now)
        RETURN user.user_id AS user_id,
               user.username AS username,
               auth_session.auth_method AS auth_method,
               auth_session.authenticated_at AS authenticated_at,
               auth_session.authenticated_at >= datetime($recent_after) AS is_recent
        """
        with self.driver.session() as session:
            record = session.run(
                query,
                token_hash=hash_session_token(token),
                now=current_time.isoformat(),
                recent_after=recent_after.isoformat(),
            ).single()
        if record is None:
            raise InvalidSessionError("登入狀態已失效，請重新登入")
        if require_recent and not record["is_recent"]:
            raise RecentAuthenticationRequiredError("請重新登入後再執行此操作")
        return {
            "user_id": record["user_id"],
            "username": record["username"],
            "auth_method": record["auth_method"],
            "authenticated_at": str(record["authenticated_at"]),
        }

    def revoke(self, token: str) -> None:
        if not token:
            return
        self._ensure_constraints()
        with self.driver.session() as session:
            session.run(
                """
                MATCH (auth_session:AuthSession {token_hash: $token_hash})
                DETACH DELETE auth_session
                """,
                token_hash=hash_session_token(token),
            ).consume()

    def revoke_all_for_user(self, user_id: str) -> None:
        if not user_id:
            return
        self._ensure_constraints()
        with self.driver.session() as session:
            session.run(
                """
                MATCH (:User {user_id: $user_id})-[:HAS_AUTH_SESSION]->(
                    auth_session:AuthSession
                )
                DETACH DELETE auth_session
                """,
                user_id=user_id,
            ).consume()


_service_instance: AuthSessionService | None = None
_service_lock = threading.Lock()


def create_auth_session_service() -> AuthSessionService:
    global _service_instance
    if _service_instance is not None:
        return _service_instance
    with _service_lock:
        if _service_instance is None:
            if os.getenv("AUTH_STORAGE_BACKEND", "postgres").strip().lower() == "postgres":
                from tools.auth.postgres_auth_session import (
                    PostgresAuthSessionService,
                )

                _service_instance = PostgresAuthSessionService()
            else:
                from database.profile_repository import profile_repository

                _service_instance = AuthSessionService(profile_repository.neo4j_driver)
    return _service_instance
