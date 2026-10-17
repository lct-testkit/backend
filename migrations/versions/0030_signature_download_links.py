"""B7-3: signature_download_links — скачивание подписанного файла/протокола по ссылке

Отдельная от токена подписания (`signature_requests.access_token_hash`) ссылка «только
скачать»: не даёт права подписывать, свой TTL, лимит скачиваний и отзыв. В БД — только
sha256 токена.

Revision ID: 0030_signature_download_links
Revises: 0029_learner_profile_crypto
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_signature_download_links"
down_revision: str | None = "0029_learner_profile_crypto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signature_download_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloads", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signature_download_links"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["signature_documents.id"],
            name="fk_signature_download_links_document_id_signature_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["signature_requests.id"],
            name="fk_signature_download_links_request_id_signature_requests",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_signature_download_links_document", "signature_download_links", ["document_id"]
    )
    op.create_index(
        "uq_signature_download_links_token_hash",
        "signature_download_links",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("signature_download_links")
