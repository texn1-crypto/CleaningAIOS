"""Add idempotent owner-approved agent replay requests."""

import sqlalchemy as sa
from alembic import op


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_replay_requests" in inspector.get_table_names():
        return
    op.create_table(
        "agent_replay_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "replay_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            sa.Integer(),
            sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("request_hash", name="uq_agent_replay_request_hash"),
        sa.UniqueConstraint("replay_task_id", name="uq_agent_replay_task"),
        sa.UniqueConstraint("approval_id", name="uq_agent_replay_approval"),
    )
    op.create_index(
        "ix_agent_replay_requests_source_run_id",
        "agent_replay_requests",
        ["source_run_id"],
    )
    op.create_index(
        "ix_agent_replay_requests_replay_task_id",
        "agent_replay_requests",
        ["replay_task_id"],
    )
    op.create_index(
        "ix_agent_replay_requests_approval_id",
        "agent_replay_requests",
        ["approval_id"],
    )
    op.create_index(
        "ix_agent_replay_requests_requested_by",
        "agent_replay_requests",
        ["requested_by"],
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agent_replay_requests" not in inspector.get_table_names():
        return
    for name in (
        "ix_agent_replay_requests_requested_by",
        "ix_agent_replay_requests_approval_id",
        "ix_agent_replay_requests_replay_task_id",
        "ix_agent_replay_requests_source_run_id",
    ):
        op.drop_index(name, table_name="agent_replay_requests")
    op.drop_table("agent_replay_requests")
