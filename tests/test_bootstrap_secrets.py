from pathlib import Path

from scripts.bootstrap_secrets import SECRET_NAMES, bootstrap


def test_bootstrap_generates_only_missing_secrets_and_is_idempotent(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "API_KEY=keep-this-value\n"
        "TELEGRAM_CALLBACK_SECRET=\n"
        "UNSUBSCRIBE_SECRET=already-configured\n"
    )

    updated = bootstrap(environment)

    assert updated == ["TELEGRAM_CALLBACK_SECRET", "PUBLIC_LEAD_RATE_SECRET"]
    values = dict(
        line.split("=", 1)
        for line in environment.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["API_KEY"] == "keep-this-value"
    assert values["UNSUBSCRIBE_SECRET"] == "already-configured"
    assert all(values[name] for name in SECRET_NAMES)
    assert environment.stat().st_mode & 0o777 == 0o600
    assert bootstrap(environment) == []
    assert "keep-this-value" in environment.read_text()
