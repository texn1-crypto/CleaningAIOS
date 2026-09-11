"""Add immutable tender supplier quote snapshots.

Revision ID: 0027
Revises: 0026
"""

from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "tender_supplier_quote_snapshots" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "tender_supplier_quote_snapshots",
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
                server_default="tender-supplier-quote-v1",
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("supplier_name", sa.String(length=255), nullable=False),
            sa.Column("supplier_identifier", sa.String(length=64), nullable=False),
            sa.Column("quote_reference", sa.String(length=255), nullable=False),
            sa.Column("quoted_at", sa.DateTime(), nullable=False),
            sa.Column("valid_until", sa.DateTime(), nullable=False),
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
                "status IN ('needs_verification', 'rejected', 'verified')",
                name="ck_tender_supplier_quote_status",
            ),
            sa.UniqueConstraint(
                "record_id",
                "input_hash",
                name="uq_tender_supplier_quote_input",
            ),
        )
        index_names = {
            "record_id": "ix_tender_supplier_quote_snapshots_record_id",
            "input_hash": "ix_tender_supplier_quote_snapshots_input_hash",
            "prequalification_snapshot_hash": (
                "ix_tender_supplier_quote_prequalification"
            ),
            "status": "ix_tender_supplier_quote_snapshots_status",
            "supplier_name": "ix_tender_supplier_quote_snapshots_supplier_name",
            "supplier_identifier": "ix_tender_supplier_quote_supplier_identifier",
            "quote_reference": "ix_tender_supplier_quote_snapshots_quote_reference",
            "quoted_at": "ix_tender_supplier_quote_snapshots_quoted_at",
            "valid_until": "ix_tender_supplier_quote_snapshots_valid_until",
            "created_by": "ix_tender_supplier_quote_snapshots_created_by",
            "created_at": "ix_tender_supplier_quote_snapshots_created_at",
        }
        for column, index_name in index_names.items():
            op.create_index(
                index_name,
                "tender_supplier_quote_snapshots",
                [column],
            )

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_tender_supplier_quote_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'tender supplier quote snapshots are immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS tender_supplier_quote_snapshots_immutable "
            "ON tender_supplier_quote_snapshots"
        )
        op.execute("""
            CREATE TRIGGER tender_supplier_quote_snapshots_immutable
            BEFORE UPDATE OR DELETE ON tender_supplier_quote_snapshots
            FOR EACH ROW EXECUTE FUNCTION prevent_tender_supplier_quote_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS tender_supplier_quote_snapshots_immutable "
            "ON tender_supplier_quote_snapshots"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS prevent_tender_supplier_quote_mutation()"
        )
    if "tender_supplier_quote_snapshots" in sa.inspect(bind).get_table_names():
        op.drop_table("tender_supplier_quote_snapshots")
