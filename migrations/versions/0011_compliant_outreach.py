"""Add consent evidence and inbound mailbox configuration."""

import sqlalchemy as sa
from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "outreach_consents" not in tables:
        op.create_table(
            "outreach_consents",
            sa.Column("address", sa.String(length=320), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="verified"),
            sa.Column("purpose", sa.String(length=128), nullable=False, server_default="commercial_outreach"),
            sa.Column("source_url", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("evidence_hash", sa.String(length=64), nullable=False),
            sa.Column("verified_by", sa.String(length=128), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('verified', 'revoked')",
                name="ck_outreach_consent_status",
            ),
            sa.ForeignKeyConstraint(["record_id"], ["business_records.id"]),
            sa.PrimaryKeyConstraint("address"),
        )
        op.create_index("ix_outreach_consents_record_id", "outreach_consents", ["record_id"])
        op.create_index("ix_outreach_consents_status", "outreach_consents", ["status"])

    columns = {column["name"] for column in inspector.get_columns("sender_mailboxes")}
    additions = (
        ("imap_host", sa.Column("imap_host", sa.String(length=255), nullable=False, server_default="")),
        ("imap_port", sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993")),
        ("imap_username", sa.Column("imap_username", sa.String(length=320), nullable=False, server_default="")),
        ("imap_secret_ref", sa.Column("imap_secret_ref", sa.String(length=255), nullable=False, server_default="")),
        ("inbound_enabled", sa.Column("inbound_enabled", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("last_imap_uid", sa.Column("last_imap_uid", sa.Integer(), nullable=False, server_default="0")),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("sender_mailboxes", column)
    indexes = {index["name"] for index in inspector.get_indexes("sender_mailboxes")}
    if "ix_sender_mailboxes_inbound_enabled" not in indexes:
        op.create_index("ix_sender_mailboxes_inbound_enabled", "sender_mailboxes", ["inbound_enabled"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sender_mailboxes" in inspector.get_table_names():
        indexes = {index["name"] for index in inspector.get_indexes("sender_mailboxes")}
        if "ix_sender_mailboxes_inbound_enabled" in indexes:
            op.drop_index("ix_sender_mailboxes_inbound_enabled", table_name="sender_mailboxes")
        columns = {column["name"] for column in inspector.get_columns("sender_mailboxes")}
        for name in ("last_imap_uid", "inbound_enabled", "imap_secret_ref", "imap_username", "imap_port", "imap_host"):
            if name in columns:
                op.drop_column("sender_mailboxes", name)
    if "outreach_consents" in inspector.get_table_names():
        op.drop_index("ix_outreach_consents_status", table_name="outreach_consents")
        op.drop_index("ix_outreach_consents_record_id", table_name="outreach_consents")
        op.drop_table("outreach_consents")
