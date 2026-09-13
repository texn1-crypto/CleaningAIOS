"""Add durable tender source collection receipts.

Revision ID: 0025
Revises: 0024
"""

from alembic import op
import sqlalchemy as sa


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "tender_source_runs" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "tender_source_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "items_seen >= 0 AND created_count >= 0 AND updated_count >= 0 "
            "AND unchanged_count >= 0",
            name="ck_tender_source_run_counts",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_tender_source_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tender_source_runs_finished_at",
        "tender_source_runs",
        ["finished_at"],
    )
    op.create_index(
        "ix_tender_source_runs_source_hash",
        "tender_source_runs",
        ["source_hash"],
    )
    op.create_index(
        "ix_tender_source_runs_status",
        "tender_source_runs",
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "tender_source_runs" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index(
        "ix_tender_source_runs_status",
        table_name="tender_source_runs",
    )
    op.drop_index(
        "ix_tender_source_runs_source_hash",
        table_name="tender_source_runs",
    )
    op.drop_index(
        "ix_tender_source_runs_finished_at",
        table_name="tender_source_runs",
    )
    op.drop_table("tender_source_runs")
