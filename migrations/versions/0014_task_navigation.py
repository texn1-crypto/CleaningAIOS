"""Add task assignment and due dates for control-center navigation."""

import sqlalchemy as sa
from alembic import op


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "assigned_to" not in columns:
        op.add_column(
            "tasks",
            sa.Column("assigned_to", sa.String(length=128), nullable=False, server_default=""),
        )
    if "due_at" not in columns:
        op.add_column("tasks", sa.Column("due_at", sa.DateTime(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("tasks")}
    if "ix_tasks_assigned_to" not in indexes:
        op.create_index("ix_tasks_assigned_to", "tasks", ["assigned_to"])
    if "ix_tasks_due_at" not in indexes:
        op.create_index("ix_tasks_due_at", "tasks", ["due_at"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("tasks")}
    if "ix_tasks_due_at" in indexes:
        op.drop_index("ix_tasks_due_at", table_name="tasks")
    if "ix_tasks_assigned_to" in indexes:
        op.drop_index("ix_tasks_assigned_to", table_name="tasks")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("tasks")}
    if "due_at" in columns:
        op.drop_column("tasks", "due_at")
    if "assigned_to" in columns:
        op.drop_column("tasks", "assigned_to")
