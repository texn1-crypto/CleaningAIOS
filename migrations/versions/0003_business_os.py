"""Business graph, goals, tender intelligence and outreach resources."""

import sqlalchemy as sa
from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = ["operating_entities", "business_goals", "decision_outcomes", "tender_documents", "sender_mailboxes", "message_templates", "import_jobs"]


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("outbound_messages")}
    if "mailbox_id" not in columns:
        op.add_column("outbound_messages", sa.Column("mailbox_id", sa.Integer(), nullable=True))
        op.create_index("ix_outbound_messages_mailbox_id", "outbound_messages", ["mailbox_id"])
    if "template_id" not in columns:
        op.add_column("outbound_messages", sa.Column("template_id", sa.Integer(), nullable=True))
    if "attachments" not in columns:
        op.add_column("outbound_messages", sa.Column("attachments", sa.JSON(), nullable=False, server_default="[]"))
    task_columns = {column["name"] for column in sa.inspect(bind).get_columns("tasks")}
    if "max_attempts" not in task_columns: op.add_column("tasks", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    if "timeout_seconds" not in task_columns: op.add_column("tasks", sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="120"))
    if "next_retry_at" not in task_columns: op.add_column("tasks", sa.Column("next_retry_at", sa.DateTime(), nullable=True))
    run_columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_runs")}
    if "evidence" not in run_columns: op.add_column("agent_runs", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    if "cost" not in run_columns: op.add_column("agent_runs", sa.Column("cost", sa.Float(), nullable=False, server_default="0"))


def downgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("outbound_messages")}
    if "attachments" in columns: op.drop_column("outbound_messages", "attachments")
    if "template_id" in columns: op.drop_column("outbound_messages", "template_id")
    if "mailbox_id" in columns:
        op.drop_index("ix_outbound_messages_mailbox_id", table_name="outbound_messages")
        op.drop_column("outbound_messages", "mailbox_id")
    for name in ["next_retry_at", "timeout_seconds", "max_attempts"]:
        if name in {column["name"] for column in sa.inspect(bind).get_columns("tasks")}: op.drop_column("tasks", name)
    for name in ["cost", "evidence"]:
        if name in {column["name"] for column in sa.inspect(bind).get_columns("agent_runs")}: op.drop_column("agent_runs", name)
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
