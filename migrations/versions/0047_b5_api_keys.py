"""B5-10: API-ключи для систем, которые не умеют OAuth2 client_credentials

Доступ роли INTEGRATION через `X-API-Key` — альтернатива токену Keycloak client_credentials
(клиент `crm-integration` заводится отдельно в realm, спринт D1). Ключ хранится только хэшем
(SHA-256, `key_hash`); `key_prefix` — первые символы для различения в списке, не секрет.

Подпадает под `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO
crm_app` из `0002_identity_sprint` автоматически.

Revision ID: 0047_b5_api_keys
Revises: 0046_b5_webhook_subscriptions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_b5_api_keys"
down_revision: str | None = "0046_b5_webhook_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_user", "api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_user", table_name="api_keys")
    op.drop_table("api_keys")
