"""B5-9: универсальные webhook-подписки на исходящие события

До этой задачи `OutboxEvent.target` знал только `lms`/`bitrix24` — зашитые в код получатели.
Эта миграция заводит `webhook_subscriptions` (кого и на что подписали, администратор задаёт URL
и список событий без релиза) и `webhook_deliveries` (очередь доставки конкретной подписке —
отдельная от `outbox_events`, потому что у одного события исходящей интеграции теперь может быть
много адресатов, а не один `target`).

Обе таблицы подпадают под `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON
TABLES TO crm_app` из `0002_identity_sprint` автоматически.

Revision ID: 0046_b5_webhook_subscriptions
Revises: 0045_learning_progress_report
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_b5_webhook_subscriptions"
down_revision: str | None = "0045_learning_progress_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column(
            "event_types",
            postgresql.ARRAY(sa.String(length=64)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Конверт `encrypt_value` (строка или `{"__enc": "fernet:v1", "ct": "..."}") — JSONB
        # вмещает оба вида значения, как уже делает `system_settings.value`.
        sa.Column("secret_encrypted", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("disabled_reason", sa.String(length=64), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_subscriptions_active", "webhook_subscriptions", ["is_active"])
    op.create_index("ix_webhook_subscriptions_created_at", "webhook_subscriptions", ["created_at"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["webhook_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','dead')",
            name="webhook_deliveries_status_valid",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_pending_retry",
        "webhook_deliveries",
        ["status", "next_retry_at"],
        postgresql_where=sa.text("status IN ('pending','failed')"),
    )
    op.create_index(
        "ix_webhook_deliveries_subscription",
        "webhook_deliveries",
        ["subscription_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_webhook_deliveries_created_at", "webhook_deliveries", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_created_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_subscription", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_pending_retry", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_subscriptions_created_at", table_name="webhook_subscriptions")
    op.drop_index("ix_webhook_subscriptions_active", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
