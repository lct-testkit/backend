"""B7-1: signature_requests.reminders_sent/last_reminded_at — напоминания о дедлайне

Revision ID: 0031_signature_reminders
Revises: 0030_signature_download_links
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_signature_reminders"
down_revision: str | None = "0030_signature_download_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signature_requests",
        sa.Column("reminders_sent", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "signature_requests",
        sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signature_requests", "last_reminded_at")
    op.drop_column("signature_requests", "reminders_sent")
