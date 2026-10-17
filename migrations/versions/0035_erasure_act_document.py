"""B6-9: data_erasure_requests.act_document_id — связывает акт с ПЭП ответственного лица

Revision ID: 0035_erasure_act_document
Revises: 0034_retention_runs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_erasure_act_document"
down_revision: str | None = "0034_retention_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_erasure_requests",
        sa.Column("act_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_data_erasure_requests_act_document_id_signature_documents",
        "data_erasure_requests",
        "signature_documents",
        ["act_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_data_erasure_requests_act_document_id_signature_documents",
        "data_erasure_requests",
        type_="foreignkey",
    )
    op.drop_column("data_erasure_requests", "act_document_id")
