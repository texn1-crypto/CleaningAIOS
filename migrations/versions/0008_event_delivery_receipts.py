"""Add versioned event envelopes and durable consumer receipts."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("domain_events")}
    additions = (
        ("event_id", sa.Column("event_id", sa.String(length=36), nullable=True)),
        ("schema_version", sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1")),
        ("correlation_id", sa.Column("correlation_id", sa.String(length=128), nullable=False, server_default="")),
        ("causation_id", sa.Column("causation_id", sa.String(length=36), nullable=False, server_default="")),
        ("actor", sa.Column("actor", sa.String(length=128), nullable=False, server_default="system")),
        ("occurred_at", sa.Column("occurred_at", sa.DateTime(), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("domain_events", column)

    events = sa.table(
        "domain_events",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.String(length=36)),
        sa.column("correlation_id", sa.String(length=128)),
        sa.column("occurred_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
    )
    for row in bind.execute(sa.select(
        events.c.id,
        events.c.event_id,
        events.c.correlation_id,
        events.c.occurred_at,
        events.c.created_at,
    )):
        event_id = row.event_id or str(uuid4())
        bind.execute(
            events.update().where(events.c.id == row.id).values(
                event_id=event_id,
                correlation_id=row.correlation_id or event_id,
                occurred_at=row.occurred_at or row.created_at,
            )
        )

    op.alter_column("domain_events", "event_id", nullable=False)
    op.alter_column("domain_events", "occurred_at", nullable=False)
    op.alter_column("domain_events", "correlation_id", server_default=None)
    inspector = sa.inspect(bind)
    unique_names = {item["name"] for item in inspector.get_unique_constraints("domain_events")}
    check_names = {item["name"] for item in inspector.get_check_constraints("domain_events")}
    index_names = {item["name"] for item in inspector.get_indexes("domain_events")}
    if "uq_domain_event_event_id" not in unique_names:
        op.create_unique_constraint("uq_domain_event_event_id", "domain_events", ["event_id"])
    if "ck_domain_event_schema_version" not in check_names:
        op.create_check_constraint("ck_domain_event_schema_version", "domain_events", "schema_version >= 1")
    if "ck_domain_event_correlation_id" not in check_names:
        op.create_check_constraint("ck_domain_event_correlation_id", "domain_events", "correlation_id <> ''")
    if "ck_domain_event_actor" not in check_names:
        op.create_check_constraint("ck_domain_event_actor", "domain_events", "actor <> ''")
    for name, column in (
        ("ix_domain_events_event_id", "event_id"),
        ("ix_domain_events_correlation_id", "correlation_id"),
        ("ix_domain_events_causation_id", "causation_id"),
        ("ix_domain_events_actor", "actor"),
        ("ix_domain_events_occurred_at", "occurred_at"),
    ):
        if name not in index_names:
            op.create_index(name, "domain_events", [column])

    if "event_consumer_receipts" not in inspector.get_table_names():
        op.create_table(
            "event_consumer_receipts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("consumer", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("result_ref", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["domain_events.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", "consumer", name="uq_event_consumer_receipt"),
            sa.CheckConstraint("status IN ('pending', 'processing', 'succeeded', 'failed')", name="ck_event_receipt_status"),
            sa.CheckConstraint("attempts >= 0", name="ck_event_receipt_attempts"),
        )
        op.create_index("ix_event_consumer_receipts_event_id", "event_consumer_receipts", ["event_id"])
        op.create_index("ix_event_consumer_receipts_consumer", "event_consumer_receipts", ["consumer"])
        op.create_index("ix_event_consumer_receipts_status", "event_consumer_receipts", ["status"])


def downgrade():
    op.drop_index("ix_event_consumer_receipts_status", table_name="event_consumer_receipts")
    op.drop_index("ix_event_consumer_receipts_consumer", table_name="event_consumer_receipts")
    op.drop_index("ix_event_consumer_receipts_event_id", table_name="event_consumer_receipts")
    op.drop_table("event_consumer_receipts")
    op.drop_index("ix_domain_events_occurred_at", table_name="domain_events")
    op.drop_index("ix_domain_events_actor", table_name="domain_events")
    op.drop_index("ix_domain_events_causation_id", table_name="domain_events")
    op.drop_index("ix_domain_events_correlation_id", table_name="domain_events")
    op.drop_index("ix_domain_events_event_id", table_name="domain_events")
    op.drop_constraint("ck_domain_event_actor", "domain_events", type_="check")
    op.drop_constraint("ck_domain_event_correlation_id", "domain_events", type_="check")
    op.drop_constraint("ck_domain_event_schema_version", "domain_events", type_="check")
    op.drop_constraint("uq_domain_event_event_id", "domain_events", type_="unique")
    for name in ("occurred_at", "actor", "causation_id", "correlation_id", "schema_version", "event_id"):
        op.drop_column("domain_events", name)
