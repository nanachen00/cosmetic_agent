"""Interactively configure development-only Gmail SMTP without echoing secrets."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import secrets
import socket
import smtplib
import ssl
import tempfile


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
SENDER_EMAIL = "selina60523@gmail.com"


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


class IPv4SMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float):
        connection = _ipv4_socket(host, port, timeout)
        return self.context.wrap_socket(connection, server_hostname=self._host)


def _updated_env(original: str, values: dict[str, str]) -> str:
    remaining = dict(values)
    output: list[str] = []
    for line in original.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output).rstrip() + "\n"


def _write_private(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.smtp.", dir=path.parent, text=True
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    print(f"寄件信箱：{SENDER_EMAIL}")
    app_password = getpass.getpass("請輸入 Gmail 應用程式密碼（畫面不會顯示）：")
    app_password = app_password.replace(" ", "").strip()
    if len(app_password) < 16:
        raise SystemExit("應用程式密碼格式不正確，未修改 .env")

    print("正在驗證 Gmail SMTP 登入……")
    context = ssl.create_default_context()
    try:
        with IPv4SMTPSSL(
            "smtp.gmail.com", 465, timeout=20, context=context
        ) as server:
            server.login(SENDER_EMAIL, app_password)
    except Exception as error:
        raise SystemExit(f"SMTP 驗證失敗，未修改 .env：{type(error).__name__}") from error

    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    values = {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "465",
        "SMTP_USERNAME": SENDER_EMAIL,
        "SMTP_PASSWORD": app_password,
        "SMTP_FROM_EMAIL": SENDER_EMAIL,
        "SMTP_USE_TLS": "false",
        "SMTP_USE_SSL": "true",
        "PASSWORD_RESET_SECRET": secrets.token_hex(32),
    }
    _write_private(ENV_PATH, _updated_env(existing, values))
    print("SMTP 驗證成功，開發環境 .env 已安全更新。")


if __name__ == "__main__":
    main()
