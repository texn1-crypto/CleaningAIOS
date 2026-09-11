"""Persist SMTP transport quarantine and cooldown state."""

import sqlalchemy as sa
from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mail_transport_states" in inspector.get_table_names():
        return
    op.create_table(
        "mail_transport_states",
        sa.Column("mailbox_key", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("reason", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_at", sa.DateTime(), nullable=True),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_mail_transport_states_status",
        "mail_transport_states",
        ["status"],
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mail_transport_states" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("mail_transport_states")}
    if "ix_mail_transport_states_status" in indexes:
        op.drop_index("ix_mail_transport_states_status", table_name="mail_transport_states")
    op.drop_table("mail_transport_states")
