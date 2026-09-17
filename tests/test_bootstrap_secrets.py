from pathlib import Path

from scripts.bootstrap_secrets import (
    CRAWL4AI_SECRET_NAME,
    CRAWL4AI_SETTINGS,
    SECRET_NAMES,
    bootstrap,
)


def test_bootstrap_generates_only_missing_secrets_and_is_idempotent(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "API_KEY=keep-this-value\n"
        "TELEGRAM_CALLBACK_SECRET=\n"
        "UNSUBSCRIBE_SECRET=already-configured\n"
    )

    updated = bootstrap(environment)

    assert updated == [
        "TELEGRAM_CALLBACK_SECRET",
        "PUBLIC_LEAD_RATE_SECRET",
        "OPENJARVIS_API_KEY",
    ]
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


def test_bootstrap_can_enable_crawl4ai_without_printing_or_replacing_its_token(tmp_path: Path):
    environment = tmp_path / ".env"
    environment.write_text(
        "CRAWL4AI_ENABLED=false\n"
        "AGENT_READ_TOOL_TIMEOUT_SECONDS=3\n"
        "AGENT_READ_TOOL_TOTAL_TIMEOUT_SECONDS=10\n"
    )

    updated = bootstrap(environment, enable_crawl4ai=True)
    values = dict(
        line.split("=", 1)
        for line in environment.read_text().splitlines()
        if line and not line.startswith("#")
    )

    assert CRAWL4AI_SECRET_NAME in updated
    assert values[CRAWL4AI_SECRET_NAME]
    first_token = values[CRAWL4AI_SECRET_NAME]
    assert all(values[name] == value for name, value in CRAWL4AI_SETTINGS.items())
    assert bootstrap(environment, enable_crawl4ai=True) == []
    assert CRAWL4AI_SECRET_NAME not in bootstrap(environment, enable_crawl4ai=True)
    assert dict(
        line.split("=", 1)
        for line in environment.read_text().splitlines()
        if line and not line.startswith("#")
    )[CRAWL4AI_SECRET_NAME] == first_token
