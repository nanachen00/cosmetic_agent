"""Password reset mail boundary with SMTP and test-only recording adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
import os
import socket
import smtplib
import ssl
from typing import Protocol


def _ipv4_socket(host: str, port: int, timeout: float):
    last_error: OSError | None = None
    for family, socktype, proto, _, address in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        connection = socket.socket(family, socktype, proto)
        connection.settimeout(timeout)
        try:
            connection.connect(address)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("找不到可用的 IPv4 SMTP 位址")


class IPv4SMTP(smtplib.SMTP):
    def _get_socket(self, host: str, port: int, timeout: float):
        return _ipv4_socket(host, port, timeout)


class IPv4SMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float):
        connection = _ipv4_socket(host, port, timeout)
        return self.context.wrap_socket(connection, server_hostname=self._host)


class PasswordResetMailError(RuntimeError):
    """Raised when a reset message cannot be delivered."""


class PasswordResetMailer(Protocol):
    def send_code(self, *, recipient: str, code: str, expires_in_minutes: int) -> None:
        """Deliver one password reset code."""


@dataclass(slots=True)
class RecordedResetMessage:
    recipient: str
    code: str
    expires_in_minutes: int


@dataclass(slots=True)
class RecordingPasswordResetMailer:
    """Test adapter that never contacts the network or writes application logs."""

    messages: list[RecordedResetMessage] = field(default_factory=list)

    def send_code(self, *, recipient: str, code: str, expires_in_minutes: int) -> None:
        self.messages.append(
            RecordedResetMessage(
                recipient=recipient,
                code=code,
                expires_in_minutes=expires_in_minutes,
            )
        )


class SmtpPasswordResetMailer:
    """SMTP adapter; credentials are loaded only from process environment."""

    def __init__(
        self,
        *,
        subject: str = "BLINK AI 會員密碼驗證碼",
        introduction: str = "你的 BLINK AI 驗證碼是：",
    ) -> None:
        required = (
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_FROM_EMAIL",
        )
        values = {name: os.getenv(name, "").strip() for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise PasswordResetMailError("寄信服務尚未設定完成")
        self._host = values["SMTP_HOST"]
        self._port = int(values["SMTP_PORT"])
        self._username = values["SMTP_USERNAME"]
        self._password = values["SMTP_PASSWORD"]
        self._sender = values["SMTP_FROM_EMAIL"]
        self._subject = subject
        self._introduction = introduction
        self._use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def send_code(self, *, recipient: str, code: str, expires_in_minutes: int) -> None:
        message = EmailMessage()
        message["Subject"] = self._subject
        message["From"] = self._sender
        message["To"] = recipient
        message.set_content(
            f"{self._introduction}{code}\n\n"
            f"驗證碼 {expires_in_minutes} 分鐘內有效，且只能使用一次。\n"
            "若不是你本人操作，請忽略此郵件。"
        )
        try:
            context = ssl.create_default_context()
            if self._use_ssl:
                with IPv4SMTPSSL(
                    self._host, self._port, timeout=20, context=context
                ) as server:
                    server.login(self._username, self._password)
                    server.send_message(message)
            else:
                with IPv4SMTP(self._host, self._port, timeout=20) as server:
                    if self._use_tls:
                        server.starttls(context=context)
                    server.login(self._username, self._password)
                    server.send_message(message)
        except Exception as error:
            raise PasswordResetMailError("驗證信寄送失敗，請稍後再試") from error
