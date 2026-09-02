"""Add provenance-aware Company Brain documents and chunks.

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("minimum_role", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("idempotency_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_knowledge_document_version"),
        sa.CheckConstraint(
            "minimum_role IN ('viewer', 'operator', 'manager', 'admin', 'owner')",
            name="ck_knowledge_document_minimum_role",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_knowledge_document_confidence",
        ),
        sa.UniqueConstraint(
            "idempotency_hash",
            name="uq_knowledge_document_idempotency",
        ),
        sa.UniqueConstraint(
            "namespace",
            "source_uri",
            "version",
            name="uq_knowledge_document_source_version",
        ),
        sa.UniqueConstraint(
            "namespace",
            "source_uri",
            "request_digest",
            name="uq_knowledge_document_source_request",
        ),
    )
    op.create_index("ix_knowledge_documents_namespace", "knowledge_documents", ["namespace"])
    op.create_index("ix_knowledge_documents_checksum", "knowledge_documents", ["checksum"])
    op.create_index(
        "ix_knowledge_documents_minimum_role",
        "knowledge_documents",
        ["minimum_role"],
    )
    op.create_index("ix_knowledge_documents_valid_until", "knowledge_documents", ["valid_until"])
    op.create_index("ix_knowledge_documents_created_by", "knowledge_documents", ["created_by"])
    op.create_index("ix_knowledge_documents_created_at", "knowledge_documents", ["created_at"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("knowledge_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("lexical_terms", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunk_index"),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_knowledge_chunk_index",
        ),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index("ix_knowledge_chunks_content_hash", "knowledge_chunks", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_content_hash", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_created_at", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_created_by", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_valid_until", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_minimum_role", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_checksum", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_namespace", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
