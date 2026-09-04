"""Repair outbound mailbox and template referential integrity."""

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _foreign_key_columns(bind) -> set[tuple[str, ...]]:
    return {
        tuple(item.get("constrained_columns") or [])
        for item in sa.inspect(bind).get_foreign_keys("outbound_messages")
    }


def upgrade():
    bind = op.get_bind()
    foreign_keys = _foreign_key_columns(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("outbound_messages") as batch:
            if ("mailbox_id",) not in foreign_keys:
                batch.create_foreign_key(
                    "fk_outbound_messages_mailbox_id", "sender_mailboxes", ["mailbox_id"], ["id"]
                )
            if ("template_id",) not in foreign_keys:
                batch.create_foreign_key(
                    "fk_outbound_messages_template_id", "message_templates", ["template_id"], ["id"]
                )
        return
    if ("mailbox_id",) not in foreign_keys:
        op.create_foreign_key(
            "fk_outbound_messages_mailbox_id",
            "outbound_messages",
            "sender_mailboxes",
            ["mailbox_id"],
            ["id"],
        )
    if ("template_id",) not in foreign_keys:
        op.create_foreign_key(
            "fk_outbound_messages_template_id",
            "outbound_messages",
            "message_templates",
            ["template_id"],
            ["id"],
        )


def downgrade():
    bind = op.get_bind()
    names = {item.get("name") for item in sa.inspect(bind).get_foreign_keys("outbound_messages")}
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("outbound_messages") as batch:
            for name in ("fk_outbound_messages_template_id", "fk_outbound_messages_mailbox_id"):
                if name in names:
                    batch.drop_constraint(name, type_="foreignkey")
        return
    for name in ("fk_outbound_messages_template_id", "fk_outbound_messages_mailbox_id"):
        if name in names:
            op.drop_constraint(name, "outbound_messages", type_="foreignkey")
