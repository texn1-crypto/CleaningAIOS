"""Add measurable orchestrator routing decisions.

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "orchestrator_decisions" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "orchestrator_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_key", sa.String(length=128), nullable=False),
        sa.Column("source_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("delegated_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("selected_agent", sa.String(length=64), nullable=False),
        sa.Column("expected_result", sa.String(length=255), nullable=False),
        sa.Column("expectation_status", sa.String(length=32), nullable=False, server_default="success_expected"),
        sa.Column("outcome_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("successful", sa.Boolean(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("measured_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "expectation_status IN ('success_expected', 'at_risk')",
            name="ck_orchestrator_decision_expectation",
        ),
        sa.CheckConstraint(
            "outcome_status IN ('pending', 'succeeded', 'expectation_missed')",
            name="ck_orchestrator_decision_outcome",
        ),
        sa.UniqueConstraint("decision_key", name="uq_orchestrator_decision_key"),
        sa.UniqueConstraint("delegated_task_id", name="uq_orchestrator_decision_delegated_task"),
    )
    for column in (
        "source_task_id",
        "delegated_task_id",
        "task_type",
        "selected_agent",
        "expectation_status",
        "outcome_status",
        "successful",
        "correlation_id",
        "created_at",
        "measured_at",
    ):
        op.create_index(
            f"ix_orchestrator_decisions_{column}",
            "orchestrator_decisions",
            [column],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "orchestrator_decisions" not in sa.inspect(bind).get_table_names():
        return
    for column in reversed(
        (
            "source_task_id",
            "delegated_task_id",
            "task_type",
            "selected_agent",
            "expectation_status",
            "outcome_status",
            "successful",
            "correlation_id",
            "created_at",
            "measured_at",
        )
    ):
        op.drop_index(f"ix_orchestrator_decisions_{column}", table_name="orchestrator_decisions")
    op.drop_table("orchestrator_decisions")
