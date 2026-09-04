"""Durable request-analysis and Codex improvement queue."""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = ["improvement_requests"]


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
