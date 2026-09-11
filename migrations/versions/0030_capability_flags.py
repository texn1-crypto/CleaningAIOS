"""Add explicit protected-action capability flags.

Revision ID: 0030
Revises: 0029
"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


PROTECTED_CAPABILITIES = (
    "agent_replay",
    "bulk_outreach",
    "contract",
    "financial",
    "hr_final",
    "legal",
    "social_publication",
    "tender_participation",
    "tender_submission",
)


def upgrade() -> None:
    bind = op.get_bind()
    if "capability_flags" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "capability_flags",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "reason", sa.String(length=500), nullable=False, server_default=""
            ),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "updated_by",
                sa.String(length=128),
                nullable=False,
                server_default="system",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint("version >= 1", name="ck_capability_flag_version"),
        )
        op.create_index(
            "ix_capability_flags_enabled", "capability_flags", ["enabled"]
        )

    capability_flags = sa.table(
        "capability_flags",
        sa.column("key", sa.String(length=64)),
        sa.column("enabled", sa.Boolean()),
        sa.column("reason", sa.String(length=500)),
        sa.column("version", sa.Integer()),
        sa.column("updated_by", sa.String(length=128)),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    existing = {
        row[0]
        for row in bind.execute(sa.select(capability_flags.c.key)).all()
    }
    created_at = datetime.utcnow()
    rows = [
        {
            "key": key,
            "enabled": True,
            "reason": "Initial compatibility flag; owner approval remains mandatory",
            "version": 1,
            "updated_by": "system",
            "created_at": created_at,
            "updated_at": created_at,
        }
        for key in PROTECTED_CAPABILITIES
        if key not in existing
    ]
    if rows:
        op.bulk_insert(capability_flags, rows)


def downgrade() -> None:
    bind = op.get_bind()
    if "capability_flags" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_capability_flags_enabled", table_name="capability_flags")
    op.drop_table("capability_flags")
