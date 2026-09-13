"""Unified inbox and content plan."""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = ["inbox_messages", "content_items"]


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
