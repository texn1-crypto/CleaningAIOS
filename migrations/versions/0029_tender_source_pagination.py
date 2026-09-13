"""Add durable tender provider pagination checkpoints.

Revision ID: 0029
Revises: 0028
"""

from alembic import op
import sqlalchemy as sa


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


RUN_COLUMNS = {
    "request_url_hash": sa.Column(
        "request_url_hash", sa.String(length=64), nullable=False, server_default=""
    ),
    "next_url_hash": sa.Column(
        "next_url_hash", sa.String(length=64), nullable=False, server_default=""
    ),
    "provider_acknowledgement_hash": sa.Column(
        "provider_acknowledgement_hash",
        sa.String(length=64),
        nullable=False,
        server_default="",
    ),
    "completeness_status": sa.Column(
        "completeness_status",
        sa.String(length=32),
        nullable=False,
        server_default="unknown",
    ),
    "declared_total": sa.Column("declared_total", sa.Integer(), nullable=True),
    "page_number": sa.Column("page_number", sa.Integer(), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "tender_source_checkpoints" not in tables:
        op.create_table(
            "tender_source_checkpoints",
            sa.Column("source_hash", sa.String(length=64), primary_key=True),
            sa.Column("source_label", sa.String(length=1024), nullable=False),
            sa.Column(
                "next_url", sa.String(length=2048), nullable=False, server_default=""
            ),
            sa.Column(
                "next_url_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "last_acknowledgement_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "last_completeness_status", sa.String(length=32), nullable=False
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "version >= 1", name="ck_tender_source_checkpoint_version"
            ),
            sa.CheckConstraint(
                "last_completeness_status IN ('partial', 'complete')",
                name="ck_tender_source_checkpoint_completeness",
            ),
        )

    existing = {
        column["name"]
        for column in sa.inspect(bind).get_columns("tender_source_runs")
    }
    for name, column in RUN_COLUMNS.items():
        if name not in existing:
            op.add_column("tender_source_runs", column)

    constraint_names = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_check_constraints("tender_source_runs")
    }
    if bind.dialect.name != "sqlite":
        if "ck_tender_source_run_completeness" not in constraint_names:
            op.create_check_constraint(
                "ck_tender_source_run_completeness",
                "tender_source_runs",
                "completeness_status IN ('unknown', 'partial', 'complete')",
            )
        if "ck_tender_source_run_page_metadata" not in constraint_names:
            op.create_check_constraint(
                "ck_tender_source_run_page_metadata",
                "tender_source_runs",
                "(declared_total IS NULL OR declared_total >= 0) AND "
                "(page_number IS NULL OR page_number >= 1)",
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "tender_source_runs" in sa.inspect(bind).get_table_names():
        if bind.dialect.name != "sqlite":
            constraint_names = {
                constraint.get("name")
                for constraint in sa.inspect(bind).get_check_constraints(
                    "tender_source_runs"
                )
            }
            for name in (
                "ck_tender_source_run_page_metadata",
                "ck_tender_source_run_completeness",
            ):
                if name in constraint_names:
                    op.drop_constraint(name, "tender_source_runs", type_="check")
        existing = {
            column["name"]
            for column in sa.inspect(bind).get_columns("tender_source_runs")
        }
        for name in reversed(tuple(RUN_COLUMNS)):
            if name in existing:
                op.drop_column("tender_source_runs", name)
    if "tender_source_checkpoints" in sa.inspect(bind).get_table_names():
        op.drop_table("tender_source_checkpoints")
