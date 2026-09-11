"""Add durable critical-alert metadata and acknowledgement state."""

import sqlalchemy as sa
from alembic import op


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("owner_notifications")}
    additions = [
        ("severity", sa.Column("severity", sa.String(length=16), nullable=False, server_default="normal")),
        ("correlation_id", sa.Column("correlation_id", sa.String(length=128), nullable=False, server_default="")),
        ("acknowledged_at", sa.Column("acknowledged_at", sa.DateTime(), nullable=True)),
        ("acknowledged_by", sa.Column("acknowledged_by", sa.String(length=128), nullable=True)),
        ("dead_lettered_at", sa.Column("dead_lettered_at", sa.DateTime(), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("owner_notifications", column)

    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("owner_notifications")
    }
    for name, column in (
        ("ix_owner_notifications_severity", "severity"),
        ("ix_owner_notifications_correlation_id", "correlation_id"),
        ("ix_owner_notifications_acknowledged_at", "acknowledged_at"),
    ):
        if name not in indexes:
            op.create_index(name, "owner_notifications", [column])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {
        index["name"]
        for index in inspector.get_indexes("owner_notifications")
    }
    for name in (
        "ix_owner_notifications_acknowledged_at",
        "ix_owner_notifications_correlation_id",
        "ix_owner_notifications_severity",
    ):
        if name in indexes:
            op.drop_index(name, table_name="owner_notifications")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("owner_notifications")}
    for name in (
        "dead_lettered_at",
        "acknowledged_by",
        "acknowledged_at",
        "correlation_id",
        "severity",
    ):
        if name in columns:
            op.drop_column("owner_notifications", name)
