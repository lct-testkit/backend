"""Модель уведомлений (spec.txt §5.7, new_spec §7.7).

Четыре таблицы с разными сроками жизни:

* `notification_templates` — админится через `/api/admin/notification-templates`,
  один код события может иметь по шаблону на каждый канал (`email`+`in_app`
  для одного и того же `USER_PASSWORD_CHANGED` — «одно событие → три канала
  с разными судьбами», дословно из dop.md §10.9 про `signature_requests`, тот
  же принцип применён здесь).
* `notifications` — логическое событие, адресованное `users.id`. Это и есть
  «входящие» для `GET /api/notifications`: строка создаётся один раз и живёт,
  пока её не прочитают или не подчистит ретеншен.
* `notification_deliveries` — попытки довести событие до конкретного канала.
  `notification_id` **нежёсткий** (`ON DELETE SET NULL`), хотя весь остальной
  репозиторий по умолчанию использует `RESTRICT` (раздел 2): new_spec,
  таблица §4.8.3, прямо требует, чтобы после обезличивания непрочитанное
  `notifications` удалялось, а `notification_deliveries` оставалась как факт
  доставки «без тела» — то есть переживала удаление родителя. Сам обработчик
  обезличивания (`erasure.execute`) в этом спринте не реализуется (см. память
  проекта), но схема сразу рассчитана на него, чтобы не потребовалась вторая
  миграция.
* `user_notification_prefs` — самостоятельная настройка на пользователя и код
  события; без записи используются каналы шаблонов по умолчанию (см.
  `service.RealNotificationService`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin, VersionMixin


class NotificationChannel(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    IN_APP = "in_app"


class NotificationPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationTemplate(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    """Шаблон одного канала одного кода события."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint("code", "channel", name="uq_notification_templates_code_channel"),
        CheckConstraint(
            "channel IN ('email','telegram','in_app')",
            name="notification_templates_channel_valid",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ru")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class Notification(UuidPkMixin, Base):
    """Логическое событие, адресованное пользователю (это и есть «in-app»)."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('normal','high','critical')", name="notifications_priority_valid"
        ),
        Index("ix_notifications_recipient_created", "recipient_id", "created_at"),
        Index(
            "ix_notifications_recipient_unread",
            "recipient_id",
            postgresql_where=text("is_read = false"),
        ),
    )

    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    template_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    is_read: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotificationDelivery(UuidPkMixin, Base):
    """Одна попытка доставки события в один канал."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('email','telegram','in_app')",
            name="notification_deliveries_channel_valid",
        ),
        CheckConstraint(
            "status IN ('pending','sent','failed','skipped')",
            name="notification_deliveries_status_valid",
        ),
        Index(
            "ix_notification_deliveries_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # SET NULL, а не обычный для репозитория RESTRICT — см. докстринг модуля:
    # запись обязана пережить удаление `notifications` при обезличивании.
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    address_masked: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserNotificationPref(UuidPkMixin, TimestampMixin, Base):
    """Настройка каналов и тихих часов на пару (пользователь, код события)."""

    __tablename__ = "user_notification_prefs"
    __table_args__ = (
        UniqueConstraint("user_id", "event_code", name="uq_user_notification_prefs_user_event"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=text("'{}'")
    )
    is_enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    quiet_hours_start: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
