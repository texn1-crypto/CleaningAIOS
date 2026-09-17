"""Add immutable company qualification profile snapshots.

Revision ID: 0028
Revises: 0027
"""

from alembic import op
import sqlalchemy as sa


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "company_profile_snapshots" not in inspector.get_table_names():
        op.create_table(
            "company_profile_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "company_requisite_id",
                sa.Integer(),
                sa.ForeignKey("company_requisites.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("company_identifier", sa.String(length=12), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "rules_version",
                sa.String(length=64),
                nullable=False,
                server_default="company-profile-v1",
            ),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=False),
            sa.Column("valid_through", sa.DateTime(), nullable=False),
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
                "status IN ('needs_verification', 'restricted', 'verified')",
                name="ck_company_profile_snapshot_status",
            ),
            sa.UniqueConstraint(
                "company_requisite_id",
                "input_hash",
                name="uq_company_profile_snapshot_input",
            ),
        )
        index_names = {
            "company_requisite_id": "ix_company_profile_snapshots_company_requisite_id",
            "company_identifier": "ix_company_profile_snapshots_company_identifier",
            "input_hash": "ix_company_profile_snapshots_input_hash",
            "status": "ix_company_profile_snapshots_status",
            "verified_at": "ix_company_profile_snapshots_verified_at",
            "valid_through": "ix_company_profile_snapshots_valid_through",
            "created_by": "ix_company_profile_snapshots_created_by",
            "created_at": "ix_company_profile_snapshots_created_at",
        }
        for column, index_name in index_names.items():
            op.create_index(index_name, "company_profile_snapshots", [column])

    prequalification_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("tender_prequalification_snapshots")
    }
    if "company_profile_snapshot_hash" not in prequalification_columns:
        op.add_column(
            "tender_prequalification_snapshots",
            sa.Column(
                "company_profile_snapshot_hash",
                sa.String(length=64),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_tender_prequal_company_profile",
            "tender_prequalification_snapshots",
            ["company_profile_snapshot_hash"],
        )

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_company_profile_snapshot_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'company profile snapshots are immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS company_profile_snapshots_immutable "
            "ON company_profile_snapshots"
        )
        op.execute("""
            CREATE TRIGGER company_profile_snapshots_immutable
            BEFORE UPDATE OR DELETE ON company_profile_snapshots
            FOR EACH ROW EXECUTE FUNCTION prevent_company_profile_snapshot_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS company_profile_snapshots_immutable "
            "ON company_profile_snapshots"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS prevent_company_profile_snapshot_mutation()"
        )
    if "tender_prequalification_snapshots" in sa.inspect(bind).get_table_names():
        columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns(
                "tender_prequalification_snapshots"
            )
        }
        if "company_profile_snapshot_hash" in columns:
            op.drop_index(
                "ix_tender_prequal_company_profile",
                table_name="tender_prequalification_snapshots",
            )
            op.drop_column(
                "tender_prequalification_snapshots",
                "company_profile_snapshot_hash",
            )
    if "company_profile_snapshots" in sa.inspect(bind).get_table_names():
        op.drop_table("company_profile_snapshots")
