"""Add expiring, versioned approvals and immutable decisions."""

import sqlalchemy as sa
from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("approval_requests")}
    if "decision_version" not in columns:
        op.add_column(
            "approval_requests",
            sa.Column("decision_version", sa.Integer(), nullable=False, server_default="1"),
        )
    if "expires_at" not in columns:
        op.add_column(
            "approval_requests",
            sa.Column("expires_at", sa.DateTime(), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("approval_requests")}
    if "ix_approval_requests_expires_at" not in indexes:
        op.create_index(
            "ix_approval_requests_expires_at",
            "approval_requests",
            ["expires_at"],
        )

    if "approval_decision_records" not in inspector.get_table_names():
        op.create_table(
            "approval_decision_records",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("approval_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("result_status", sa.String(length=32), nullable=False),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False, server_default="api"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("request_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "action IN ('approve', 'reject', 'request_changes', 'expire')",
                name="ck_approval_decision_record_action",
            ),
            sa.ForeignKeyConstraint(
                ["approval_id"], ["approval_requests.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "approval_id", name="uq_approval_decision_record_approval"
            ),
        )
        op.create_index(
            "ix_approval_decision_records_approval_id",
            "approval_decision_records",
            ["approval_id"],
        )
        op.create_index(
            "ix_approval_decision_records_result_status",
            "approval_decision_records",
            ["result_status"],
        )
        op.create_index(
            "ix_approval_decision_records_actor",
            "approval_decision_records",
            ["actor"],
        )
        op.create_index(
            "ix_approval_decision_records_channel",
            "approval_decision_records",
            ["channel"],
        )

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_approval_decision_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'approval decision history is immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS approval_decision_records_immutable "
            "ON approval_decision_records"
        )
        op.execute("""
            CREATE TRIGGER approval_decision_records_immutable
            BEFORE UPDATE OR DELETE ON approval_decision_records
            FOR EACH ROW EXECUTE FUNCTION prevent_approval_decision_mutation()
        """)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS approval_decision_records_immutable "
            "ON approval_decision_records"
        )
        op.execute("DROP FUNCTION IF EXISTS prevent_approval_decision_mutation()")
    if "approval_decision_records" in inspector.get_table_names():
        op.drop_table("approval_decision_records")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("approval_requests")}
    if "ix_approval_requests_expires_at" in indexes:
        op.drop_index("ix_approval_requests_expires_at", table_name="approval_requests")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("approval_requests")}
    if "expires_at" in columns:
        op.drop_column("approval_requests", "expires_at")
    if "decision_version" in columns:
        op.drop_column("approval_requests", "decision_version")
