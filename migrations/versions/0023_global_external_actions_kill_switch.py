"""Add the global protected-external-actions kill switch.

Revision ID: 0023
Revises: 0022
"""

from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "safety_controls" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "safety_controls",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_safety_control_version"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_safety_controls_active",
        "safety_controls",
        ["active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "safety_controls" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_safety_controls_active", table_name="safety_controls")
    op.drop_table("safety_controls")
