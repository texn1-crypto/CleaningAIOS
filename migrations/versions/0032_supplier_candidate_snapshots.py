"""Add immutable supplier candidate snapshots and optional quote binding.

Revision ID: 0032
Revises: 0031
"""

from alembic import op
import sqlalchemy as sa


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tender_supplier_candidate_snapshots" not in inspector.get_table_names():
        op.create_table(
            "tender_supplier_candidate_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "record_id",
                sa.Integer(),
                sa.ForeignKey("business_records.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "prequalification_snapshot_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column(
                "rules_version",
                sa.String(length=64),
                nullable=False,
                server_default="tender-supplier-candidates-v1",
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("candidate_count", sa.Integer(), nullable=False),
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
                "status IN ('needs_verification', 'rejected', "
                "'ready_for_quote_collection')",
                name="ck_tender_supplier_candidate_status",
            ),
            sa.CheckConstraint(
                "candidate_count >= 2",
                name="ck_tender_supplier_candidate_count",
            ),
            sa.UniqueConstraint(
                "record_id",
                "input_hash",
                name="uq_tender_supplier_candidate_input",
            ),
        )
        indexes = {
            "record_id": "ix_tender_supplier_candidate_snapshots_record_id",
            "input_hash": "ix_tender_supplier_candidate_snapshots_input_hash",
            "prequalification_snapshot_hash": (
                "ix_tender_supplier_candidate_prequalification"
            ),
            "status": "ix_tender_supplier_candidate_snapshots_status",
            "created_by": "ix_tender_supplier_candidate_snapshots_created_by",
            "created_at": "ix_tender_supplier_candidate_snapshots_created_at",
        }
        for column, index_name in indexes.items():
            op.create_index(
                index_name,
                "tender_supplier_candidate_snapshots",
                [column],
            )

    quote_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("tender_supplier_quote_snapshots")
    }
    if "supplier_candidate_snapshot_hash" not in quote_columns:
        op.add_column(
            "tender_supplier_quote_snapshots",
            sa.Column(
                "supplier_candidate_snapshot_hash",
                sa.String(length=64),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_tender_supplier_quote_candidate_snapshot",
            "tender_supplier_quote_snapshots",
            ["supplier_candidate_snapshot_hash"],
        )

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_tender_supplier_candidate_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'tender supplier candidate snapshots are immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS tender_supplier_candidate_snapshots_immutable "
            "ON tender_supplier_candidate_snapshots"
        )
        op.execute("""
            CREATE TRIGGER tender_supplier_candidate_snapshots_immutable
            BEFORE UPDATE OR DELETE ON tender_supplier_candidate_snapshots
            FOR EACH ROW EXECUTE FUNCTION prevent_tender_supplier_candidate_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS tender_supplier_candidate_snapshots_immutable "
            "ON tender_supplier_candidate_snapshots"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS prevent_tender_supplier_candidate_mutation()"
        )
    quote_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("tender_supplier_quote_snapshots")
    }
    if "supplier_candidate_snapshot_hash" in quote_columns:
        op.drop_index(
            "ix_tender_supplier_quote_candidate_snapshot",
            table_name="tender_supplier_quote_snapshots",
        )
        op.drop_column(
            "tender_supplier_quote_snapshots",
            "supplier_candidate_snapshot_hash",
        )
    if "tender_supplier_candidate_snapshots" in sa.inspect(bind).get_table_names():
        op.drop_table("tender_supplier_candidate_snapshots")
