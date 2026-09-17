"""Add the separately controlled voice-call capability.

Revision ID: 0034
Revises: 0033
"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "capability_flags" not in sa.inspect(bind).get_table_names():
        return
    flags = sa.table(
        "capability_flags",
        sa.column("key", sa.String(length=64)),
        sa.column("enabled", sa.Boolean()),
        sa.column("reason", sa.String(length=500)),
        sa.column("version", sa.Integer()),
        sa.column("updated_by", sa.String(length=128)),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    exists = bind.execute(
        sa.select(flags.c.key).where(flags.c.key == "voice_call")
    ).first()
    if exists is None:
        created_at = datetime.utcnow()
        op.bulk_insert(
            flags,
            [
                {
                    "key": "voice_call",
                    "enabled": True,
                    "reason": (
                        "Initial compatibility flag; owner approval remains mandatory"
                    ),
                    "version": 1,
                    "updated_by": "system",
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            ],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "capability_flags" not in sa.inspect(bind).get_table_names():
        return
    bind.execute(
        sa.text("DELETE FROM capability_flags WHERE key = :key"),
        {"key": "voice_call"},
    )
