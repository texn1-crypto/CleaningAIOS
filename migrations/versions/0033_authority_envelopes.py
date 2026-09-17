"""Add bounded owner authority envelopes and append-only usage receipts.

Revision ID: 0033
Revises: 0032
"""

from alembic import op
import sqlalchemy as sa


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "authority_envelopes" not in tables:
        op.create_table(
            "authority_envelopes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("envelope_key", sa.String(length=128), nullable=False),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            ),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("limits", sa.JSON(), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("approved_by", sa.String(length=128), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("revoked_by", sa.String(length=128), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column(
                "revocation_reason", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "status IN ('active', 'revoked', 'expired')",
                name="ck_authority_envelope_status",
            ),
            sa.CheckConstraint(
                "expires_at > starts_at",
                name="ck_authority_envelope_time_window",
            ),
            sa.UniqueConstraint(
                "envelope_key", name="uq_authority_envelope_key"
            ),
        )
        for name, columns in (
            ("ix_authority_envelopes_envelope_key", ["envelope_key"]),
            ("ix_authority_envelopes_action", ["action"]),
            ("ix_authority_envelopes_status", ["status"]),
            ("ix_authority_envelopes_input_hash", ["input_hash"]),
            ("ix_authority_envelopes_starts_at", ["starts_at"]),
            ("ix_authority_envelopes_expires_at", ["expires_at"]),
            ("ix_authority_envelopes_approved_by", ["approved_by"]),
            ("ix_authority_envelopes_created_at", ["created_at"]),
        ):
            op.create_index(name, "authority_envelopes", columns)
    if "authority_envelope_uses" not in tables:
        op.create_table(
            "authority_envelope_uses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "envelope_id",
                sa.Integer(),
                sa.ForeignKey("authority_envelopes.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("context_digest", sa.String(length=64), nullable=False),
            sa.Column(
                "amount",
                sa.Numeric(precision=18, scale=2),
                nullable=False,
                server_default="0",
            ),
            sa.Column("unit_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column(
                "occurred_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "unit_count >= 1", name="ck_authority_envelope_use_units"
            ),
            sa.CheckConstraint(
                "amount >= 0", name="ck_authority_envelope_use_amount"
            ),
            sa.UniqueConstraint(
                "idempotency_key", name="uq_authority_envelope_use_key"
            ),
        )
        for name, columns in (
            ("ix_authority_envelope_uses_envelope_id", ["envelope_id"]),
            ("ix_authority_envelope_uses_task_id", ["task_id"]),
            ("ix_authority_envelope_uses_action", ["action"]),
            ("ix_authority_envelope_uses_actor", ["actor"]),
            ("ix_authority_envelope_uses_occurred_at", ["occurred_at"]),
        ):
            op.create_index(name, "authority_envelope_uses", columns)
    outbound_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("outbound_messages")
    }
    if "authority_envelope_use_id" not in outbound_columns:
        envelope_use_column = sa.Column(
            "authority_envelope_use_id",
            sa.Integer(),
            sa.ForeignKey("authority_envelope_uses.id", ondelete="RESTRICT"),
            nullable=True,
        )
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("outbound_messages") as batch_op:
                batch_op.add_column(envelope_use_column)
                batch_op.create_index(
                    "ix_outbound_messages_authority_envelope_use_id",
                    ["authority_envelope_use_id"],
                )
        else:
            op.add_column("outbound_messages", envelope_use_column)
            op.create_index(
                "ix_outbound_messages_authority_envelope_use_id",
                "outbound_messages",
                ["authority_envelope_use_id"],
            )
    else:
        outbound_inspector = sa.inspect(bind)
        outbound_indexes = {
            index["name"]
            for index in outbound_inspector.get_indexes("outbound_messages")
        }
        envelope_use_foreign_key_present = any(
            foreign_key.get("constrained_columns")
            == ["authority_envelope_use_id"]
            for foreign_key in outbound_inspector.get_foreign_keys(
                "outbound_messages"
            )
        )
        index_missing = (
            "ix_outbound_messages_authority_envelope_use_id"
            not in outbound_indexes
        )
        if bind.dialect.name == "sqlite" and (
            index_missing or not envelope_use_foreign_key_present
        ):
            with op.batch_alter_table("outbound_messages") as batch_op:
                if not envelope_use_foreign_key_present:
                    batch_op.create_foreign_key(
                        "fk_outbound_messages_authority_envelope_use_id",
                        "authority_envelope_uses",
                        ["authority_envelope_use_id"],
                        ["id"],
                        ondelete="RESTRICT",
                    )
                if index_missing:
                    batch_op.create_index(
                        "ix_outbound_messages_authority_envelope_use_id",
                        ["authority_envelope_use_id"],
                    )
        elif bind.dialect.name != "sqlite":
            if not envelope_use_foreign_key_present:
                op.create_foreign_key(
                    "fk_outbound_messages_authority_envelope_use_id",
                    "outbound_messages",
                    "authority_envelope_uses",
                    ["authority_envelope_use_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
            if index_missing:
                op.create_index(
                    "ix_outbound_messages_authority_envelope_use_id",
                    "outbound_messages",
                    ["authority_envelope_use_id"],
                )
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_authority_envelope_use_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'authority envelope usage receipts are immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute(
            "DROP TRIGGER IF EXISTS authority_envelope_uses_immutable "
            "ON authority_envelope_uses"
        )
        op.execute("""
            CREATE TRIGGER authority_envelope_uses_immutable
            BEFORE UPDATE OR DELETE ON authority_envelope_uses
            FOR EACH ROW EXECUTE FUNCTION prevent_authority_envelope_use_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS authority_envelope_uses_immutable "
            "ON authority_envelope_uses"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS prevent_authority_envelope_use_mutation()"
        )
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "outbound_messages" in tables:
        outbound_columns = {
            column["name"] for column in inspector.get_columns("outbound_messages")
        }
        if "authority_envelope_use_id" in outbound_columns:
            outbound_indexes = {
                index["name"]
                for index in inspector.get_indexes("outbound_messages")
            }
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table("outbound_messages") as batch_op:
                    if "ix_outbound_messages_authority_envelope_use_id" in outbound_indexes:
                        batch_op.drop_index(
                            "ix_outbound_messages_authority_envelope_use_id"
                        )
                    batch_op.drop_column("authority_envelope_use_id")
            else:
                if "ix_outbound_messages_authority_envelope_use_id" in outbound_indexes:
                    op.drop_index(
                        "ix_outbound_messages_authority_envelope_use_id",
                        table_name="outbound_messages",
                    )
                op.drop_column("outbound_messages", "authority_envelope_use_id")
    if "authority_envelope_uses" in tables:
        op.drop_table("authority_envelope_uses")
    if "authority_envelopes" in tables:
        op.drop_table("authority_envelopes")
