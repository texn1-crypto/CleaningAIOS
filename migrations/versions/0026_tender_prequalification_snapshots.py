"""Add immutable tender prequalification snapshots.

Revision ID: 0026
Revises: 0025
"""

from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "tender_prequalification_snapshots" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "tender_prequalification_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "record_id",
                sa.Integer(),
                sa.ForeignKey("business_records.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "rules_version",
                sa.String(length=64),
                nullable=False,
                server_default="tender-prequalification-v1",
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("input_snapshot", sa.JSON(), nullable=False),
            sa.Column("result_snapshot", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status IN ('needs_verification', 'ineligible', 'eligible')",
                name="ck_tender_prequalification_status",
            ),
            sa.UniqueConstraint(
                "record_id",
                "input_hash",
                name="uq_tender_prequalification_input",
            ),
        )
        for column in (
            "record_id",
            "input_hash",
            "status",
            "created_by",
            "created_at",
        ):
            op.create_index(
                f"ix_tender_prequalification_snapshots_{column}",
                "tender_prequalification_snapshots",
                [column],
            )

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_tender_prequalification_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'tender prequalification snapshots are immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS tender_prequalification_snapshots_immutable "
            "ON tender_prequalification_snapshots"
        )
        op.execute("""
            CREATE TRIGGER tender_prequalification_snapshots_immutable
            BEFORE UPDATE OR DELETE ON tender_prequalification_snapshots
            FOR EACH ROW EXECUTE FUNCTION prevent_tender_prequalification_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS tender_prequalification_snapshots_immutable "
            "ON tender_prequalification_snapshots"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS prevent_tender_prequalification_mutation()"
        )
    if "tender_prequalification_snapshots" in sa.inspect(bind).get_table_names():
        op.drop_table("tender_prequalification_snapshots")
