"""Complete bank requisite details for supplier invoices."""

import sqlalchemy as sa
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("company_requisites")}
    columns = [
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="RUB"),
        sa.Column("bank_inn", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("bank_address", sa.String(length=500), nullable=False, server_default=""),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("company_requisites", column)


def downgrade():
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("company_requisites")}
    for name in ("bank_address", "bank_inn", "currency"):
        if name in existing:
            op.drop_column("company_requisites", name)
