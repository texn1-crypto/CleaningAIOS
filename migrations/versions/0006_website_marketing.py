"""Public website, media workflow, requisites and owner notifications."""

from alembic import op

from app.db import Base
from app import models  # noqa: F401

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = ["company_requisites", "owner_notifications", "media_assets"]


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
