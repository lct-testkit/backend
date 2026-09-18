"""Спринт 9: интеграции (new_spec §4.14, §7.8)

Создаёт шесть таблиц (`integration_sources`, `inbound_messages`,
`outbox_events`, `external_refs`, `sync_cursors`, `learning_progress`) —
раздел 9 («Порядок реализации») ставит интеграции на моках сразу после
отчётности и дашбордов (шаг 8, наш спринт 8), до администрирования (шаг 9).

`outbox_events` заменяет собой только *реализацию* интерфейса
`integration.service.OutboxService` — сам интерфейс и вызовы
`get_outbox_service().publish(...)` из `crm.service`/`signing.service`
существовали до этого спринта и писали только в структурный лог
(`LoggingOutboxService`). Таблицы не было вовсе — этот файл заводит её
впервые, а не переносит данные из промежуточного состояния.

`Permission.INTEGRATION_INGEST`/`INTEGRATION_ADMIN` (`app/core/permissions.py`)
и `cms_webhook_secret_ref`/`lms_base_url`/`lms_auth_ref`/
`bitrix_connector_enabled` (`app/core/config.py`) уже существовали до этого
спринта — то же предварительное резервирование прав/конфигурации, что и в
предыдущих спринтах (например, `Permission.REPORT_READ` до модуля
`reporting`).

Revision ID: 0010_integration_sprint
Revises: 0009_reporting_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_integration_sprint"
down_revision: str | None = "0009_reporting_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=True),
        sa.Column("auth_type", sa.String(16), nullable=True),
        sa.Column("credentials_ref", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            onupdate=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_sources"),
        sa.UniqueConstraint("code", name="uq_integration_sources_code"),
        sa.CheckConstraint(
            "code IN ('cms','lms','bitrix24')", name="ck_integration_sources_code_valid"
        ),
    )
    op.create_index(
        "ix_integration_sources_created_at", "integration_sources", ["created_at"]
    )

    op.create_table(
        "inbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_code", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("message_type", sa.String(64), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'received'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resulting_entity_type", sa.String(32), nullable=True),
        sa.Column("resulting_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inbound_messages"),
        sa.UniqueConstraint(
            "source_code", "external_id", name="uq_inbound_messages_source_external"
        ),
        sa.CheckConstraint(
            "status IN ('received','processed','failed','duplicate')",
            name="ck_inbound_messages_status_valid",
        ),
    )
    op.create_index("ix_inbound_messages_received_at", "inbound_messages", ["received_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("target", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','dead')", name="ck_outbox_events_status_valid"
        ),
    )
    op.create_index("ix_outbox_events_created_at", "outbox_events", ["created_at"])
    op.create_index(
        "ix_outbox_events_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"]
    )
    op.create_index(
        "ix_outbox_events_pending_retry", "outbox_events", ["status", "next_retry_at"],
        postgresql_where=sa.text("status IN ('pending','failed')"),
    )

    op.create_table(
        "external_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_code", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("synced_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_direction", sa.String(8), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_external_refs"),
        sa.UniqueConstraint(
            "source_code", "entity_type", "external_id",
            name="uq_external_refs_source_entity_external",
        ),
        sa.UniqueConstraint(
            "source_code", "entity_type", "entity_id", name="uq_external_refs_source_entity_our"
        ),
        sa.CheckConstraint(
            "sync_direction IN ('inbound','outbound')",
            name="ck_external_refs_sync_direction_valid",
        ),
    )

    op.create_table(
        "sync_cursors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_code", sa.String(16), nullable=False),
        sa.Column("resource", sa.String(64), nullable=False),
        sa.Column("cursor_value", sa.String(255), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            onupdate=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_cursors"),
        sa.UniqueConstraint("source_code", "resource", name="uq_sync_cursors_source_resource"),
    )

    op.create_table(
        "learning_progress",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_course_id", sa.String(255), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_pct", sa.Integer(), nullable=True),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raw", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_progress"),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"],
            name="fk_learning_progress_contact_id_contacts", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_learning_progress_deal_id_deals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_learning_progress_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "deal_id", "external_course_id", name="uq_learning_progress_deal_course"
        ),
        sa.CheckConstraint(
            "progress_pct IS NULL OR (progress_pct >= 0 AND progress_pct <= 100)",
            name="ck_learning_progress_pct_range",
        ),
    )
    op.create_index("ix_learning_progress_contact", "learning_progress", ["contact_id"])


def downgrade() -> None:
    op.drop_table("learning_progress")
    op.drop_table("sync_cursors")
    op.drop_table("external_refs")
    op.drop_index("ix_outbox_events_pending_retry", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_inbound_messages_received_at", table_name="inbound_messages")
    op.drop_table("inbound_messages")
    op.drop_index("ix_integration_sources_created_at", table_name="integration_sources")
    op.drop_table("integration_sources")
