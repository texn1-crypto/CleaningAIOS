from pathlib import Path

import pytest

from scripts.set_mail_password import PASSWORD_NAMES, configure


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
