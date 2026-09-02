"""Add policy-controlled read-only agent tool call audit records."""

import sqlalchemy as sa
from alembic import op


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_tool_calls" in inspector.get_table_names():
        return
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_type", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="read_only"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("input_digest", sa.String(length=71), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("result_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("mode = 'read_only'", name="ck_agent_tool_call_mode"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'denied', 'timed_out', 'failed')",
            name="ck_agent_tool_call_status",
        ),
    )
    for column in (
        "agent_run_id",
        "task_id",
        "agent_type",
        "tool_name",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_agent_tool_calls_{column}",
            "agent_tool_calls",
            [column],
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_tool_calls" not in inspector.get_table_names():
        return
    for column in reversed(
        (
            "agent_run_id",
            "task_id",
            "agent_type",
            "tool_name",
            "status",
            "created_at",
        )
    ):
        op.drop_index(f"ix_agent_tool_calls_{column}", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")
