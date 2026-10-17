"""B7-4: signature_documents.on_signed/requested_in_status_id — автопереход после подписи

Revision ID: 0033_signature_on_signed
Revises: 0032_deal_signature_expired
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_signature_on_signed"
down_revision: str | None = "0032_deal_signature_expired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signature_documents", sa.Column("on_signed", sa.String(64), nullable=True))
    op.add_column(
        "signature_documents",
        sa.Column("requested_in_status_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_signature_documents_requested_in_status_id_workflow_statuses",
        "signature_documents",
        "workflow_statuses",
        ["requested_in_status_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_signature_documents_requested_in_status_id_workflow_statuses",
        "signature_documents",
        type_="foreignkey",
    )
    op.drop_column("signature_documents", "requested_in_status_id")
    op.drop_column("signature_documents", "on_signed")
