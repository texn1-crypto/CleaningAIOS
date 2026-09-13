"""Add explicit orchestrator decision outcomes.

Revision ID: 0022
Revises: 0021
"""

from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("orchestrator_decisions")
    }
    if "decision_outcome" in columns:
        return
    op.add_column(
        "orchestrator_decisions",
        sa.Column(
            "decision_outcome",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.execute(
        "UPDATE orchestrator_decisions SET decision_outcome = "
        "CASE WHEN successful IS TRUE THEN 'success' "
        "WHEN successful IS FALSE THEN 'fail' ELSE 'pending' END"
    )
    op.create_index(
        "ix_orchestrator_decisions_decision_outcome",
        "orchestrator_decisions",
        ["decision_outcome"],
    )
    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_orchestrator_decision_explicit_outcome",
            "orchestrator_decisions",
            "decision_outcome IN ('pending', 'success', 'partial', 'fail')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("orchestrator_decisions")
    }
    if "decision_outcome" not in columns:
        return
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_orchestrator_decision_explicit_outcome",
            "orchestrator_decisions",
            type_="check",
        )
    op.drop_index(
        "ix_orchestrator_decisions_decision_outcome",
        table_name="orchestrator_decisions",
    )
    op.drop_column("orchestrator_decisions", "decision_outcome")
