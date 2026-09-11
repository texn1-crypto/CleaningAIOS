"""Scope tender external identities to their provider source.

Revision ID: 0024
Revises: 0023
"""

from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraint_names = {
        item["name"]
        for item in inspector.get_unique_constraints("business_records")
    }
    index_names = {
        item["name"] for item in inspector.get_indexes("business_records")
    }
    if "uq_record_external" in constraint_names:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(
                "business_records",
                recreate="always",
            ) as batch_op:
                batch_op.drop_constraint(
                    "uq_record_external",
                    type_="unique",
                )
        else:
            op.drop_constraint(
                "uq_record_external",
                "business_records",
                type_="unique",
            )
    if "uq_record_external_non_tender" not in index_names:
        op.create_index(
            "uq_record_external_non_tender",
            "business_records",
            ["record_type", "external_id"],
            unique=True,
            postgresql_where=sa.text(
                "record_type <> 'tender' AND external_id IS NOT NULL"
            ),
            sqlite_where=sa.text(
                "record_type <> 'tender' AND external_id IS NOT NULL"
            ),
        )
    if "uq_tender_provider_external" not in index_names:
        op.create_index(
            "uq_tender_provider_external",
            "business_records",
            ["source", "external_id"],
            unique=True,
            postgresql_where=sa.text(
                "record_type = 'tender' AND external_id IS NOT NULL"
            ),
            sqlite_where=sa.text(
                "record_type = 'tender' AND external_id IS NOT NULL"
            ),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_tender_provider_external",
        table_name="business_records",
    )
    op.drop_index(
        "uq_record_external_non_tender",
        table_name="business_records",
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "business_records",
            recreate="always",
        ) as batch_op:
            batch_op.create_unique_constraint(
                "uq_record_external",
                ["record_type", "external_id"],
            )
    else:
        op.create_unique_constraint(
            "uq_record_external",
            "business_records",
            ["record_type", "external_id"],
        )
