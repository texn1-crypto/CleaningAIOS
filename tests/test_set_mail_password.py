from pathlib import Path

import pytest

from scripts.set_mail_password import PASSWORD_NAMES, configure, configure_smtp


def test_mail_password_is_written_to_all_transports_without_changing_other_values(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "SMTP_PASSWORD=\n"
        "API_KEY=keep-me\n"
        "SMTP_PASSWORD=\n"
    )

    configure(environment, "application-password")

    lines = environment.read_text().splitlines()
    assert lines.count("SMTP_PASSWORD=application-password") == 2
    assert "IMAP_MAILRU_PASSWORD=application-password" in lines
    assert "API_KEY=keep-me" in lines
    assert environment.stat().st_mode & 0o777 == 0o600
    assert all(any(line.startswith(f"{name}=") for line in lines) for name in PASSWORD_NAMES)


@pytest.mark.parametrize("password", ["short", "bad\npassword"])
def test_mail_password_rejects_invalid_values(tmp_path: Path, password: str):
    environment = tmp_path / ".env"
    environment.write_text("SMTP_PASSWORD=\n")

    with pytest.raises(ValueError):
        configure(environment, password)


def test_gmail_smtp_configuration_updates_sender_without_touching_mailru_imap(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "SMTP_HOST=smtp.mail.ru\n"
        "SMTP_PORT=465\n"
        "SMTP_USERNAME=old@mail.ru\n"
        "SMTP_PASSWORD=old-password\n"
        "SMTP_FROM_EMAIL=old@mail.ru\n"
        "IMAP_MAILRU_PASSWORD=keep-existing-imap\n"
    )

    configure_smtp(environment, "gmail", "Sender@Gmail.com", "abcd efgh ijkl mnop")

    lines = environment.read_text().splitlines()
    assert "SMTP_HOST=smtp.gmail.com" in lines
    assert "SMTP_PORT=587" in lines
    assert "SMTP_USERNAME=sender@gmail.com" in lines
    assert "SMTP_PASSWORD=abcdefghijklmnop" in lines
    assert "SMTP_FROM_EMAIL=sender@gmail.com" in lines
    assert "IMAP_MAILRU_PASSWORD=keep-existing-imap" in lines
    assert environment.stat().st_mode & 0o777 == 0o600


def test_mailru_configuration_uses_ssl_and_updates_inbound_password(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text("SMTP_HOST=\nSMTP_PORT=\nIMAP_MAILRU_PASSWORD=\n")

    configure_smtp(environment, "mailru", "Sender@Mail.ru", "abcd efgh ijkl mnop")

    lines = environment.read_text().splitlines()
    assert "SMTP_HOST=smtp.mail.ru" in lines
    assert "SMTP_PORT=465" in lines
    assert "SMTP_USERNAME=sender@mail.ru" in lines
    assert "SMTP_PASSWORD=abcdefghijklmnop" in lines
    assert "SMTP_FROM_EMAIL=sender@mail.ru" in lines
    assert "SMTP_MAILRU_PASSWORD=abcdefghijklmnop" in lines
    assert "IMAP_MAILRU_USERNAME=sender@mail.ru" in lines
    assert "IMAP_MAILRU_PASSWORD=abcdefghijklmnop" in lines


@pytest.mark.parametrize(
    ("provider", "email", "password"),
    [
        ("unknown", "sender@gmail.com", "abcdefghijklmnop"),
        ("gmail", "not-an-email", "abcdefghijklmnop"),
        ("gmail", "sender@gmail.com", "short"),
    ],
)
def test_smtp_configuration_rejects_invalid_values(
    tmp_path: Path, provider: str, email: str, password: str
):
    environment = tmp_path / ".env"
    environment.write_text("SMTP_PASSWORD=\n")

    with pytest.raises(ValueError):
        configure_smtp(environment, provider, email, password)
