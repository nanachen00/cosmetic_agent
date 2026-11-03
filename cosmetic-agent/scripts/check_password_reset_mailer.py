"""No-network test for the password reset mail boundary."""

from tools.auth.password_reset_mailer import RecordingPasswordResetMailer


def main() -> None:
    mailer = RecordingPasswordResetMailer()
    mailer.send_code(
        recipient="recipient@example.test",
        code="123456",
        expires_in_minutes=10,
    )
    assert len(mailer.messages) == 1
    message = mailer.messages[0]
    assert message.recipient == "recipient@example.test"
    assert message.code == "123456"
    assert message.expires_in_minutes == 10
    print("recording_mailer=true fixed_test_code=true network_contacted=false")


if __name__ == "__main__":
    main()
