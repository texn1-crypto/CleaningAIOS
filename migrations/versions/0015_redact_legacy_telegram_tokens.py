"""Redact Telegram credentials from legacy persisted error payloads."""

import sqlalchemy as sa
from alembic import op


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


URL_PATTERN = r"(https://api\.telegram\.org/bot)[^/\"[:space:]]+"
BARE_TOKEN_PATTERN = r"[0-9]{6,12}:[A-Za-z0-9_-]{20,}"
URL_REPLACEMENT = r"\1[TELEGRAM_TOKEN_REDACTED]"
TOKEN_REPLACEMENT = "[TELEGRAM_TOKEN_REDACTED]"


TEXT_COLUMNS = {
    "agent_runs": ["error"],
    "agent_states": ["last_error"],
    "domain_events": ["last_error"],
    "event_consumer_receipts": ["last_error"],
    "improvement_requests": [
        "request_text",
        "reason",
        "codex_prompt",
        "implementation_summary",
        "last_error",
    ],
    "outbound_messages": ["error"],
    "owner_notifications": ["body", "last_error"],
    "tasks": ["description"],
}

JSON_COLUMNS = {
    "agent_runs": ["input", "output", "evidence"],
    "agent_states": ["metrics"],
    "audit_logs": ["details"],
    "domain_events": ["payload", "metadata_json"],
    "improvement_requests": [
        "intent",
        "missing_capabilities",
        "acceptance_criteria",
        "test_plan",
        "test_evidence",
    ],
    "owner_notifications": ["data"],
    "tasks": ["payload", "result"],
}


def _redacted(expression: str) -> str:
    return (
        "regexp_replace("
        f"regexp_replace({expression}, :url_pattern, :url_replacement, 'g'), "
        ":token_pattern, :token_replacement, 'g')"
    )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    parameters = {
        "url_pattern": URL_PATTERN,
        "url_replacement": URL_REPLACEMENT,
        "token_pattern": BARE_TOKEN_PATTERN,
        "token_replacement": TOKEN_REPLACEMENT,
    }
    for table, columns in TEXT_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column not in existing:
                continue
            quoted_column = f'"{column}"'
            statement = sa.text(
                f'UPDATE "{table}" SET "{column}" = {_redacted(quoted_column)} '
                f'WHERE "{column}" LIKE :url_match OR "{column}" ~ :token_pattern'
            )
            bind.execute(statement, {**parameters, "url_match": "%api.telegram.org/bot%"})
    for table, columns in JSON_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column not in existing:
                continue
            expression = f'"{column}"::text'
            statement = sa.text(
                f'UPDATE "{table}" SET "{column}" = ({_redacted(expression)})::json '
                f'WHERE "{column}"::text LIKE :url_match OR "{column}"::text ~ :token_pattern'
            )
            bind.execute(statement, {**parameters, "url_match": "%api.telegram.org/bot%"})


def downgrade():
    # Redaction is intentionally irreversible: credentials must never be restored.
    pass
