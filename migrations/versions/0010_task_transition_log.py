"""Add the task state machine transition ledger."""

import sqlalchemy as sa
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_transitions" not in inspector.get_table_names():
        op.create_table(
            "task_transitions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("from_status", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("to_status", sa.String(length=32), nullable=False),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("correlation_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("details", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("transition_key", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "to_status IN ('open', 'queued', 'running', 'blocked', 'done', 'failed')",
                name="ck_task_transition_to_status",
            ),
            sa.CheckConstraint(
                "from_status IN ('', 'open', 'queued', 'running', 'blocked', 'done', 'failed')",
                name="ck_task_transition_from_status",
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("transition_key", name="uq_task_transition_key"),
        )
        for name, column in (
            ("ix_task_transitions_task_id", "task_id"),
            ("ix_task_transitions_to_status", "to_status"),
            ("ix_task_transitions_actor", "actor"),
            ("ix_task_transitions_correlation_id", "correlation_id"),
            ("ix_task_transitions_created_at", "created_at"),
        ):
            op.create_index(name, "task_transitions", [column])

    tasks = sa.table(
        "tasks",
        sa.column("id", sa.Integer()),
        sa.column("status", sa.String(length=32)),
        sa.column("created_at", sa.DateTime()),
    )
    transitions = sa.table(
        "task_transitions",
        sa.column("task_id", sa.Integer()),
        sa.column("from_status", sa.String(length=32)),
        sa.column("to_status", sa.String(length=32)),
        sa.column("actor", sa.String(length=128)),
        sa.column("reason", sa.String(length=255)),
        sa.column("correlation_id", sa.String(length=128)),
        sa.column("details", sa.JSON()),
        sa.column("transition_key", sa.String(length=255)),
        sa.column("created_at", sa.DateTime()),
    )
    recorded = set(bind.execute(sa.select(transitions.c.task_id)).scalars())
    for task in bind.execute(sa.select(tasks.c.id, tasks.c.status, tasks.c.created_at)):
        if task.id in recorded:
            continue
        bind.execute(transitions.insert().values(
            task_id=task.id,
            from_status="",
            to_status=task.status,
            actor="migration",
            reason="existing_task_backfill",
            correlation_id="",
            details={},
            transition_key=f"task:{task.id}:created",
            created_at=task.created_at,
        ))

    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_task_transition_mutation()
            RETURNS trigger AS $function$
            BEGIN
                RAISE EXCEPTION 'task transition history is immutable';
            END;
            $function$ LANGUAGE plpgsql
        """)
        op.execute("DROP TRIGGER IF EXISTS task_transitions_immutable ON task_transitions")
        op.execute("""
            CREATE TRIGGER task_transitions_immutable
            BEFORE UPDATE OR DELETE ON task_transitions
            FOR EACH ROW EXECUTE FUNCTION prevent_task_transition_mutation()
        """)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS task_transitions_immutable ON task_transitions")
        op.execute("DROP FUNCTION IF EXISTS prevent_task_transition_mutation()")
    if "task_transitions" in sa.inspect(bind).get_table_names():
        op.drop_table("task_transitions")
