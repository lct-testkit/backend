"""Спринт 7: уведомления (spec.txt §5.7/§6.11, new_spec §7.7)

Создаёт четыре таблицы домена уведомлений: `notification_templates`,
`notifications`, `notification_deliveries`, `user_notification_prefs`.

Ни одна другая таблица не меняется: в отличие от предыдущих спринтов здесь
нет «голых» UUID-колонок предыдущих миграций, ожидающих этого спринта —
модуль `notification` до сих пор существовал только как логирующая заглушка
(`app/modules/notification/service.py`, см. её докстринг), на которую уже
ссылались 11 мест в 7 модулях (`identity`, `crm`, `catalog`, `registry`,
`signing`), поэтому таблицам не с чем было связываться внешними ключами
заранее.

**`notification_deliveries.notification_id` — единственный FK в этой
миграции с `ON DELETE SET NULL`**, а не `RESTRICT` по умолчанию для
репозитория (раздел 2). Причина — таблица ретеншена new_spec §4.8.3:
непрочитанные `notifications` при обезличивании пользователя удаляются, а
`notification_deliveries` обязана пережить это удаление и остаться «фактом
доставки без тела» (нужно для реестра трансграничной/сторонней передачи,
ст. 21 152-ФЗ). Сам обработчик обезличивания (`erasure.execute`) в этот
спринт не входит и делается отдельно, но схема рассчитана на него сразу,
чтобы не потребовалась вторая миграция под один и тот же вопрос.

`ix_organizations_name_trgm` в diff автогенерации — предсуществующий индекс
`organizations`, созданный сырым SQL в 0005 (а не через `Index(...)` в
модели), поэтому автогенерация неизбежно предлагает его удалить. Это не
имеет отношения к уведомлениям и в миграцию не включено.

Revision ID: 0008_notification_sprint
Revises: 0007_signing_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_notification_sprint"
down_revision: str | None = "0007_signing_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("subject_template", sa.Text(), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("locale", sa.String(8), server_default=sa.text("'ru'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_templates"),
        sa.UniqueConstraint("code", "channel", name="uq_notification_templates_code_channel"),
        sa.CheckConstraint(
            "channel IN ('email','telegram','in_app')",
            name="ck_notification_templates_notification_templates_channel_valid",
        ),
    )
    op.create_index("ix_notification_templates_code", "notification_templates", ["code"])
    op.create_index(
        "ix_notification_templates_created_at", "notification_templates", ["created_at"]
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("priority", sa.String(16), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name="fk_notifications_recipient_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "priority IN ('normal','high','critical')",
            name="ck_notifications_notifications_priority_valid",
        ),
    )
    op.create_index(
        "ix_notifications_recipient_created", "notifications", ["recipient_id", "created_at"]
    )
    op.create_index(
        "ix_notifications_recipient_unread",
        "notifications",
        ["recipient_id"],
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index("ix_notifications_template_code", "notifications", ["template_code"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("address_masked", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_deliveries"),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_deliveries_notification_id_notifications",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "channel IN ('email','telegram','in_app')",
            name="ck_notification_deliveries_notification_deliveries_channel_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','skipped')",
            name="ck_notification_deliveries_notification_deliveries_status_valid",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_deliveries_pending",
        "notification_deliveries",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "user_notification_prefs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_code", sa.String(64), nullable=False),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("quiet_hours_start", sa.Time(), nullable=True),
        sa.Column("quiet_hours_end", sa.Time(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_notification_prefs"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_notification_prefs_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("user_id", "event_code", name="uq_user_notification_prefs_user_event"),
    )
    op.create_index(
        "ix_user_notification_prefs_created_at", "user_notification_prefs", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("user_notification_prefs")
    op.drop_table("notification_deliveries")
    op.drop_table("notifications")
    op.drop_table("notification_templates")
