"""Protect generated tender application manifests from row mutation.

Revision ID: 0031
Revises: 0030
"""

from alembic import op


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_tender_application_manifest_mutation()
        RETURNS trigger AS $function$
        BEGIN
            IF OLD.analysis->>'kind' = 'application_evidence_manifest' THEN
                RAISE EXCEPTION 'tender application manifests are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $function$ LANGUAGE plpgsql
    """)
    op.execute(
        "DROP TRIGGER IF EXISTS tender_application_manifests_immutable "
        "ON tender_documents"
    )
    op.execute("""
        CREATE TRIGGER tender_application_manifests_immutable
        BEFORE UPDATE OR DELETE ON tender_documents
        FOR EACH ROW EXECUTE FUNCTION prevent_tender_application_manifest_mutation()
    """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "DROP TRIGGER IF EXISTS tender_application_manifests_immutable "
        "ON tender_documents"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_tender_application_manifest_mutation()"
    )
