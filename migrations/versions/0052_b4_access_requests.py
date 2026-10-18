"""B4-12 (A-18): запрос доступа к чужой записи

Менеджер пытается завести вуз, который уже ведёт коллега, получает 409 CRM-1302 и упирается в
тупик — ни способа попросить доступ, ни следа в журнале. `access_requests` даёт временный доступ
(до `expires_at`), без переноса владения: `organizations.owner_id` не меняется,
`catalog.service.organization_scope_clause_for_rules` добавляет отдельную ветку по этой таблице.

Подпадает под `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO
crm_app` из `0002_identity_sprint` автоматически.

Revision ID: 0052_b4_access_requests
Revises: 0048_b4_org_drift_dismissed
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052_b4_access_requests"
down_revision: str | None = "0051_b4_org_drift_dismissed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["requester_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "entity_type IN ('organization','deal')", name="access_requests_entity_type_valid"
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','cancelled','revoked')",
            name="access_requests_status_valid",
        ),
    )
    op.create_index(
        "uq_access_requests_pending",
        "access_requests",
        ["requester_id", "entity_type", "entity_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_access_requests_grant",
        "access_requests",
        ["requester_id", "entity_type", "status", "expires_at"],
    )
    op.create_index(
        "ix_access_requests_entity", "access_requests", ["entity_type", "entity_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_access_requests_entity", table_name="access_requests")
    op.drop_index("ix_access_requests_grant", table_name="access_requests")
    op.drop_index("uq_access_requests_pending", table_name="access_requests")
    op.drop_table("access_requests")
